#!/usr/bin/env python3
"""
AWS Secrets Loader — KellyFindomBot
=====================================
Pulls all bot secrets from AWS Secrets Manager at container startup,
injects them as environment variables, then exec's the actual bot process.

This keeps ALL secrets out of code, Docker images, and env files.
AWS IAM role attached to ECS task controls access — zero static creds in container.

Usage (entrypoint):
    python aws_secrets_loader.py [bot args...]
"""

import os
import sys
import json
import logging

logger = logging.getLogger("secrets_loader")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [SECRETS] %(message)s")

SECRET_NAME = os.getenv("KELLY_SECRET_NAME", "kellyfindombot/prod/secrets")
AWS_REGION  = os.getenv("AWS_REGION", "us-east-1")
USE_AWS     = os.getenv("USE_AWS_SECRETS", "true").lower() == "true"
S3_MEDIA_BUCKET = os.getenv("S3_MEDIA_BUCKET", "")
S3_SESSION_KEY = os.getenv("S3_SESSION_KEY", "session/kelly_session.session")
TELEGRAM_SESSION_FILE = os.getenv("TELEGRAM_SESSION_FILE", "kelly_session.session")
KELLY_PERSONA_NAMES = ("kelly", "findom")


def _first_present(secrets: dict, *keys: str, default: str = "") -> str:
    """Return first non-empty secret value, checking keys in the given order."""
    for key in keys:
        val = secrets.get(key)
        if val is not None and str(val).strip() != "":
            return str(val)
    return default


def _from_secret_or_env(secrets: dict, secret_key: str, env_key: str, default: str = "") -> str:
    """Resolve value with precedence: secret_key/lower, secret_key/upper, env, fallback."""
    return _first_present(
        secrets,
        secret_key,
        secret_key.upper(),
        default=os.getenv(env_key, default),
    )


def load_from_secrets_manager() -> dict:
    """Fetch all secrets from AWS Secrets Manager and return as dict."""
    try:
        import boto3
        from botocore.exceptions import ClientError

        client = boto3.client("secretsmanager", region_name=AWS_REGION)
        response = client.get_secret_value(SecretId=SECRET_NAME)
        secret_str = response.get("SecretString", "{}")
        cfg = json.loads(secret_str)
        logger.info("✓ AWS Secrets Manager loaded successfully")
        return cfg
    except ImportError:
        logger.error("boto3 not installed — cannot load from Secrets Manager")
        sys.exit(1)
    except ClientError as e:
        code = e.response["Error"]["Code"]
        logger.error(f"Secrets Manager error [{code}]: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to load secrets: {e}")
        sys.exit(1)


def inject_env(secrets: dict):
    """Inject secrets as environment variables for the child process."""
    mapping = {
        # Telegram
        "TELEGRAM_API_ID": _from_secret_or_env(secrets, "telegram_api_id", "TELEGRAM_API_ID"),
        "TELEGRAM_API_HASH": _from_secret_or_env(secrets, "telegram_api_hash", "TELEGRAM_API_HASH"),
        "ADMIN_USER_ID": _from_secret_or_env(secrets, "admin_user_id", "ADMIN_USER_ID"),
        "BOT_PERSONA": _from_secret_or_env(secrets, "bot_persona", "BOT_PERSONA", default="kelly"),
        "ENABLE_MONETIZATION": _from_secret_or_env(
            secrets, "enable_monetization", "ENABLE_MONETIZATION", default="true"
        ),
        # Payment bot
        "PAYMENT_BOT_TOKEN": _from_secret_or_env(secrets, "payment_bot_token", "PAYMENT_BOT_TOKEN"),
        "PAYMENT_BOT_USERNAME": _from_secret_or_env(secrets, "payment_bot_username", "PAYMENT_BOT_USERNAME"),
        # ElevenLabs TTS
        "ELEVENLABS_API_KEY": _from_secret_or_env(secrets, "elevenlabs_api_key", "ELEVENLABS_API_KEY"),
        "ELEVENLABS_VOICE_ID": _from_secret_or_env(secrets, "elevenlabs_voice_id", "ELEVENLABS_VOICE_ID"),
        # Monitoring
        "MONITOR_AUTH_TOKEN": _from_secret_or_env(secrets, "monitor_auth_token", "MONITOR_AUTH_TOKEN"),
        # ComfyUI / image gen
        "COMFYUI_FACE_IMAGE": _from_secret_or_env(secrets, "comfyui_face_image", "COMFYUI_FACE_IMAGE"),
        # LLM endpoints
        "TEXT_AI_PORT": _from_secret_or_env(secrets, "text_ai_port", "TEXT_AI_PORT", default="1234"),
        "IMAGE_AI_PORT": _from_secret_or_env(secrets, "image_ai_port", "IMAGE_AI_PORT", default="11434"),
        "TTS_PORT": _from_secret_or_env(secrets, "tts_port", "TTS_PORT", default="5001"),
    }
    for key, val in mapping.items():
        if val:
            os.environ[key] = str(val)
    logger.info("✓ Environment variables injected from secrets")


def maybe_restore_session_from_s3():
    """Restore Telethon session from S3 when running in AWS and session is missing."""
    if os.path.exists(TELEGRAM_SESSION_FILE):
        logger.info("✓ Telegram session file already present")
        return

    if not S3_MEDIA_BUCKET:
        logger.warning("S3_MEDIA_BUCKET not set and no local session file found")
        return

    try:
        import boto3
        from botocore.exceptions import ClientError

        logger.info(f"Downloading session from s3://{S3_MEDIA_BUCKET}/{S3_SESSION_KEY}")
        s3 = boto3.client("s3", region_name=AWS_REGION)
        s3.download_file(S3_MEDIA_BUCKET, S3_SESSION_KEY, TELEGRAM_SESSION_FILE)
        logger.info(f"✓ Session restored to {TELEGRAM_SESSION_FILE}")
    except ImportError:
        logger.error("boto3 not installed — cannot restore session from S3")
        sys.exit(1)
    except ClientError as e:
        code = e.response["Error"].get("Code", "Unknown")
        logger.error(f"S3 session restore failed [{code}]: {e}")
        sys.exit(1)
    except Exception as e:
        logger.error(f"Failed to restore Telegram session from S3: {e}")
        sys.exit(1)


def validate_runtime_config():
    """Fail fast on runtime configs that would break monetized Kelly go-live."""
    monetization_on = os.getenv("ENABLE_MONETIZATION", "true").lower() == "true"
    kelly_mode = os.getenv("BOT_PERSONA", "").lower() in KELLY_PERSONA_NAMES
    payment_token = os.getenv("PAYMENT_BOT_TOKEN", "").strip()

    if kelly_mode and monetization_on and not payment_token:
        logger.error(
            "PAYMENT_BOT_TOKEN is required when BOT_PERSONA is kelly/findom and ENABLE_MONETIZATION=true; "
            "refusing to start because users would be gated but unable to pay."
        )
        sys.exit(1)


def main():
    bot_args = sys.argv[1:]  # everything after this script name

    if USE_AWS:
        secrets = load_from_secrets_manager()
        inject_env(secrets)
        maybe_restore_session_from_s3()
    else:
        logger.info("USE_AWS_SECRETS=false — reading from environment / .env directly")

    validate_runtime_config()

    # Exec the bot process (replaces this process — PID 1 in container)
    cmd = [sys.executable, "kelly_telegram_bot.py"] + bot_args
    logger.info(f"Launching bot: {' '.join(cmd)}")
    os.execv(sys.executable, cmd)


if __name__ == "__main__":
    main()
