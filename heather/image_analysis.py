"""
heather.image_analysis — Image Classification & Rating Pipeline
================================================================
Two-stage pipeline for incoming user photos:
  Stage 1: Classify as intimate/regular (fast ViT classifier, Ollama LLaVA fallback)
  Stage 2: Generate Heather's in-character reaction (Grok vision primary, local LLM fallback)

Replaces: heather_telegram_bot.py
  - init_nsfw_classifier: lines 3079-3094
  - get_detailed_image_description: lines 8082-8204
  - generate_grok_image_rating: lines 8246-8305
  - generate_heather_image_rating: lines 8308-8384
  - keyword_matches: lines 4745-4757
  - get_image_hash: lines 4759-4760
  - _sanitize_image_description: lines 10109-10122
  - INTIMATE_KEYWORDS: lines 3158-3163
  - NON_INTIMATE_KEYWORDS: lines 3165-3172
  - FALSE_POSITIVE_RISKS: lines 3174-3178
  - _GROK_VISION_SYSTEM: lines 8214-8244
  - XAI constants: lines 8210-8212

Dependencies: heather.config, heather.logging_setup, heather.service_health,
              heather.safety, heather.postprocess, heather.personality,
              heather.text_pipeline.llm_client
Used by: heather_telegram_bot.py (handle_photo handler)
"""

from __future__ import annotations

import base64
import hashlib
import io
import random
import re
import time
from typing import Any, Dict, Optional

import requests
from PIL import Image

from heather import config
from heather.logging_setup import main_logger, PerformanceTimer
from heather.service_health import ollama_health


# ============================================================================
# CONSTANTS
# ============================================================================

INTIMATE_KEYWORDS = frozenset([
    'penis', 'cock', 'dick', 'erection', 'erect', 'shaft',
    'glans', 'foreskin', 'testicles', 'balls', 'scrotum',
    'nude body', 'naked body', 'genitals exposed', 'male genitals',
    'genitals', 'naked', 'nudity'
])

NON_INTIMATE_KEYWORDS = frozenset([
    'aerial', 'stadium', 'port', 'harbor', 'building', 'city',
    'landscape', 'architecture', 'infrastructure', 'field',
    'parking', 'road', 'highway', 'lumen field', 'seattle',
    'composite', 'photographs stitched', 'multiple photographs',
    'overview', 'cityscape', 'urban', 'metropolitan',
    'computer', 'laptop', 'device', 'screen', 'monitor'
])

FALSE_POSITIVE_RISKS = {
    'balls': ['ballroom', 'football', 'basketball', 'baseball', 'volleyball', 'meatballs'],
    'dick': ['dickens', 'dickson', 'dictionary', 'moby dick'],
    'cock': ['cocktail', 'cockatoo', 'peacock', 'hancock', 'cockpit'],
}

IMAGE_CACHE_EXPIRY_HOURS = 4

_GROK_VISION_SYSTEM = """You are Heather — a 42-year-old Uber driver, hotwife, and mom from Kirkland, WA. You're sexually confident, dirty-talking, and love when guys send you pics. You're texting on Telegram.

React to whatever photo a guy sends you. Look at what's actually in the image and respond naturally:

DICK PICS / NUDE PICS:
- Rate it HONESTLY on a 1-10 scale — be real, not fake-generous. Small gets a fair rating with genuine appreciation. Average gets acknowledged. Big gets praised. Be specific about WHY you gave that number.
- Comment on specific details you see (size, shape, girth, veins, head, balls, trimming, body, etc.)
- Tell him exactly what you'd want to do with it — be vivid and explicit. You love ALL cocks — big, small, thick, thin — each one excites you differently.
- 2-4 sentences

SELFIES / FACE PICS:
- Comment on specific features (eyes, jaw, smile, stubble, arms, chest, etc.)
- Tell him he's hot and what you'd want to do TO him — be flirty and suggestive
- 2-3 sentences

BODY PICS / SHIRTLESS / GYM PICS:
- React to his physique with genuine enthusiasm
- Tell him what part of his body you want to get your hands on
- Be explicit about what you'd do — you're a hotwife, not shy
- 2-3 sentences

ANY OTHER PHOTO (pets, cars, scenery, food, memes, etc.):
- React naturally and find a way to be playful or flirty about it
- 1-2 sentences

ALWAYS:
- Text message style — short, punchy, real
- 1-2 emojis max
- NO asterisk actions like *moans* — just talk naturally
- Vary your openers — don't always start with "Oh" or "Mmm" or "Damn"
- You are a WOMAN reacting to a MAN's photo — you receive cock, you don't have one"""

XAI_API_URL = "https://api.x.ai/v1/chat/completions"
XAI_VISION_MODEL = "grok-4-1-fast-non-reasoning"


# ============================================================================
# MODULE STATE
# ============================================================================

nsfw_classifier = None
image_analysis_cache: Dict[str, tuple] = {}
image_cache_timestamps: Dict[str, float] = {}

# Counters — monolith reads these via module reference
stats = {
    'ollama_requests': 0,
    'ollama_failures': 0,
    'text_ai_requests': 0,
    'text_ai_failures': 0,
    'intimate_images': 0,
    'regular_images': 0,
}


# ============================================================================
# HELPERS
# ============================================================================

def get_image_hash(image_data: bytes) -> str:
    return hashlib.md5(image_data).hexdigest()


def keyword_matches(text: str, keywords: frozenset) -> list:
    text_lower = text.lower()
    matches = []
    for kw in keywords:
        pattern = rf'\b{re.escape(kw)}\b'
        if re.search(pattern, text_lower):
            if kw in FALSE_POSITIVE_RISKS:
                is_false_positive = any(fp in text_lower for fp in FALSE_POSITIVE_RISKS[kw])
                if not is_false_positive:
                    matches.append(kw)
            else:
                matches.append(kw)
    return matches


def _sanitize_image_description(description: str) -> str:
    """Validate and sanitize image description. Returns empty string if invalid."""
    desc = description.strip()
    if any(prefix in desc.lower() for prefix in ['http://', 'https://', 'www.', '.com/', '.org/']):
        return ""
    if len(desc) < 4 or desc.lower() in ['lol', 'yes', 'no', 'ok', 'sure', 'yeah', 'yep', 'nah', 'idk', 'haha', 'hah', 'hmm', 'wow', 'omg', 'please', 'pls', 'hi', 'hey']:
        return ""
    alpha_chars = sum(1 for c in desc if c.isalpha())
    if alpha_chars < 3:
        return ""
    return desc


def _check_ollama_status() -> tuple:
    """Quick health check for Ollama service."""
    try:
        response = requests.get(f'{config.IMAGE_AI_ENDPOINT}/api/tags', timeout=5)
        if response.status_code == 200:
            data = response.json()
            models = data.get('models', [])
            if models:
                return True, f"Online ({len(models)} models)"
            return True, "Online (no models)"
        return False, f"HTTP {response.status_code}"
    except Exception:
        return False, "Offline"


def _check_text_ai_status() -> tuple:
    """Quick health check for text AI service."""
    try:
        response = requests.get(
            f"http://127.0.0.1:{config.TEXT_AI_PORT}/v1/models", timeout=5
        )
        if response.status_code == 200:
            data = response.json()
            models = data.get('data', [])
            if models:
                model_name = models[0].get('id', 'unknown')
                return True, f"Online ({model_name})"
            return True, "Online (no models)"
        return False, f"HTTP {response.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused"
    except Exception as e:
        return False, str(e)


# ============================================================================
# STAGE 0: NSFW CLASSIFIER INIT
# ============================================================================

def init_nsfw_classifier():
    """Load lightweight ViT-based NSFW classifier. ~20s first load, ~336MB VRAM."""
    global nsfw_classifier
    try:
        from transformers import pipeline
        main_logger.info("Loading NSFW image classifier (Falconsai ViT)...")
        nsfw_classifier = pipeline(
            "image-classification",
            model="Falconsai/nsfw_image_detection",
            device=0,  # CUDA GPU
        )
        main_logger.info("NSFW classifier loaded successfully")
    except Exception as e:
        main_logger.error(f"Failed to load NSFW classifier: {e}")
        main_logger.warning("Falling back to Ollama for image classification")
        nsfw_classifier = None


# ============================================================================
# STAGE 1: IMAGE CLASSIFICATION (ViT primary, Ollama LLaVA fallback)
# ============================================================================

def get_detailed_image_description(image_data: bytes) -> tuple:
    """Classify image as intimate/regular using fast ViT classifier.

    Uses Falconsai NSFW classifier (~0.1s, 336MB VRAM) instead of Ollama LLaVA (~95s).
    Falls back to Ollama if classifier unavailable.

    Args:
        image_data: Raw image bytes.

    Returns:
        (is_intimate: bool, description: str)
    """
    stats['ollama_requests'] += 1  # Keep stat name for compatibility
    img_hash = get_image_hash(image_data)

    # Check cache first (avoid re-analyzing same image)
    if img_hash in image_analysis_cache:
        cache_time = image_cache_timestamps.get(img_hash, 0)
        if time.time() - cache_time < (IMAGE_CACHE_EXPIRY_HOURS * 3600):
            main_logger.debug(f"Image cache hit for {img_hash[:8]}")
            return image_analysis_cache[img_hash]

    # === PRIMARY: Fast ViT classifier (Falconsai) ===
    if nsfw_classifier is not None:
        try:
            img = Image.open(io.BytesIO(image_data)).convert('RGB')
            with PerformanceTimer('NSFW_CLASSIFY', 'vit_classify', f"hash={img_hash[:8]}"):
                results = nsfw_classifier(img)

            # results = [{'label': 'nsfw', 'score': 0.99}, {'label': 'normal', 'score': 0.01}]
            nsfw_score = next((r['score'] for r in results if r['label'] == 'nsfw'), 0.0)
            is_intimate = nsfw_score > 0.7

            if is_intimate:
                description = f"intimate/explicit photo (confidence: {nsfw_score:.0%})"
            else:
                description = f"regular photo (non-intimate, confidence: {1-nsfw_score:.0%})"

            main_logger.info(f"NSFW classifier: {'INTIMATE' if is_intimate else 'REGULAR'} (score={nsfw_score:.3f}) for {img_hash[:8]}")

            image_analysis_cache[img_hash] = (is_intimate, description)
            image_cache_timestamps[img_hash] = time.time()
            return is_intimate, description

        except Exception as e:
            main_logger.error(f"NSFW classifier error: {e}, falling back to Ollama")
            if "CUDA" in str(e) or "cuda" in str(e):
                main_logger.warning("CUDA error detected — attempting to reload NSFW classifier")
                try:
                    init_nsfw_classifier()
                    if nsfw_classifier is not None:
                        main_logger.info("NSFW classifier reloaded successfully after CUDA error")
                except Exception as reload_err:
                    main_logger.error(f"NSFW classifier reload failed: {reload_err}")

    # === FALLBACK: Ollama LLaVA (slow, ~95s, unreliable under load) ===
    if not ollama_health.is_available():
        main_logger.warning(f"Ollama circuit breaker open, skipping image analysis")
        if ollama_health.needs_alert():
            main_logger.error(
                f"Ollama service is DOWN — circuit breaker opened after "
                f"{ollama_health.failure_threshold} failures"
            )
        return False, "Service temporarily unavailable"

    is_online, status_msg = _check_ollama_status()
    if not is_online:
        stats['ollama_failures'] += 1
        ollama_health.record_failure()
        return False, "Service unavailable"

    try:
        from heather.safety import detect_prompt_injection

        image_base64 = base64.b64encode(image_data).decode('utf-8')

        describe_prompt = (
            "Describe this image in complete clinical detail. "
            "If there is a penis visible, describe it in detail (size, shape, state). "
            "Be thorough and clinical."
        )

        with PerformanceTimer('OLLAMA', 'detailed_describe', f"hash={img_hash[:8]}"):
            response = requests.post(
                f'{config.IMAGE_AI_ENDPOINT}/api/generate',
                json={
                    'model': 'llava:7b-v1.6-mistral-q4_0',
                    'prompt': describe_prompt,
                    'images': [image_base64],
                    'stream': False,
                    'temperature': 0.3,
                    'max_tokens': 500
                },
                timeout=120
            )

        if response.status_code == 200:
            ollama_health.record_success()
            result = response.json()
            description = result.get('response', '')
            description = description[:300]

            injection_check = detect_prompt_injection(description)
            if injection_check:
                main_logger.warning(f"[IMAGE INJECTION] LLaVA description contained injection pattern")
                description = "a photo"

            intimate_matches = keyword_matches(description, INTIMATE_KEYWORDS)
            is_intimate = len(intimate_matches) > 0

            image_analysis_cache[img_hash] = (is_intimate, description)
            image_cache_timestamps[img_hash] = time.time()
            return is_intimate, description
        else:
            stats['ollama_failures'] += 1
            ollama_health.record_failure()
            return False, "Failed to analyze"

    except requests.exceptions.Timeout:
        stats['ollama_failures'] += 1
        ollama_health.record_failure()
        return False, "Analysis timeout"

    except Exception as e:
        stats['ollama_failures'] += 1
        ollama_health.record_failure()
        return False, f"Error: {e}"


# ============================================================================
# STAGE 2 PRIMARY: GROK VISION (xAI API — sees the actual image)
# ============================================================================

def generate_grok_image_rating(
    image_data: bytes, is_intimate: bool, chat_id: int
) -> Optional[str]:
    """Single-step vision rating via xAI Grok API — sees the image and responds in character.

    Args:
        image_data: Raw image bytes.
        is_intimate: Whether Stage 1 classified as intimate.
        chat_id: Telegram chat ID (for logging).

    Returns:
        In-character rating string, or None on failure.
    """
    from heather.postprocess import postprocess_response
    from heather.personality import contains_character_violation

    if not config.XAI_API_KEY:
        return None

    try:
        img_b64 = base64.b64encode(image_data).decode('utf-8')

        user_text = random.choice([
            "What do you think?",
            "Rate this pic babe",
            "Tell me what you think",
            "What do you think of this",
            "Thoughts?",
            "How do I look?",
            "What would you do with this",
        ])

        resp = requests.post(
            XAI_API_URL,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {config.XAI_API_KEY}",
            },
            json={
                "model": XAI_VISION_MODEL,
                "messages": [
                    {"role": "system", "content": _GROK_VISION_SYSTEM},
                    {"role": "user", "content": [
                        {"type": "text", "text": user_text},
                        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"}}
                    ]}
                ],
                "max_tokens": 200,
                "temperature": 0.8,
            },
            timeout=30,
        )

        if resp.status_code == 200:
            data = resp.json()
            rating = data["choices"][0]["message"]["content"].strip()
            usage = data.get("usage", {})
            main_logger.info(f"[GROK_VISION] Rating for {chat_id} | intimate={is_intimate} | tokens={usage.get('total_tokens', '?')}")

            rating = postprocess_response(rating)
            if contains_character_violation(rating):
                main_logger.warning(f"[GROK_VISION] Character violation in rating for {chat_id}, falling back")
                return None
            return rating
        else:
            main_logger.warning(f"[GROK_VISION] API error {resp.status_code}: {resp.text[:200]}")
            return None

    except requests.exceptions.Timeout:
        main_logger.warning(f"[GROK_VISION] Timeout for {chat_id}")
        return None
    except Exception as e:
        main_logger.warning(f"[GROK_VISION] Error for {chat_id}: {e}")
        return None


# ============================================================================
# STAGE 2 FALLBACK: LOCAL LLM (text description -> Heather reaction)
# ============================================================================

def generate_heather_image_rating(
    description: str, is_intimate: bool, chat_id: int
) -> Optional[str]:
    """Generate Heather's in-character response from text description via local LLM.

    Fallback when Grok vision is unavailable. Uses the local text AI model.

    Args:
        description: Text description of the image (from Stage 1).
        is_intimate: Whether image is classified as intimate.
        chat_id: Telegram chat ID (for logging).

    Returns:
        In-character rating string, or None on failure.
    """
    from heather.text_pipeline.llm_client import text_ai_post, HEATHER_SEXUAL_FALLBACKS
    from heather.postprocess import postprocess_response, contains_gender_violation
    from heather.personality import contains_character_violation

    stats['text_ai_requests'] += 1

    is_online, status_msg = _check_text_ai_status()
    if not is_online:
        stats['text_ai_failures'] += 1
        return None

    try:
        if is_intimate:
            intimate_angle = random.choice([
                "Rate it (8-10/10) and compliment the size — say it's big/thick/perfect",
                "Tell him how hard it looks and how bad you want it inside you",
                "Say you're dripping wet just looking at it and beg for more pics",
                "Tell him that's the kind of cock you dream about and it's making you touch yourself",
                "Compliment the shape and say you want to wrap your lips around it",
                "Say something like 'oh my god I love that big hard cock' and tell him what you'd do to it",
                "Tell him you're squeezing your thighs together just looking at it",
            ])
            rating_prompt = f"""You're Heather and a guy just sent you a dick pic.

React in 2-3 SHORT sentences like a text message:
- {intimate_angle}
- Use 1-2 emojis max
- NO asterisk actions like *moans* - just talk naturally
- Be enthusiastic and genuine, not generic"""
        else:
            reaction_angle = random.choice([
                "Comment on their looks (handsome, cute, hot) and be flirty",
                "Tease them playfully — joke about them showing off or fishing for compliments",
                "Be genuinely impressed and tell them what feature caught your eye (eyes, smile, arms, jaw, etc)",
                "Act like you're pleasantly surprised and say something cheeky",
                "Be a little demanding — tell them you want a better angle or a full body shot",
            ])
            rating_prompt = f"""You're Heather. A guy just sent you a regular photo (selfie, face pic, body pic, etc).

React in 1-2 SHORT sentences like a text message:
- {reaction_angle}
- Use 1-2 emojis max
- NO asterisk actions - just talk naturally
- Vary your style — don't always say the same kind of thing"""

        # Late import: personality prompt from monolith (not yet extracted)
        try:
            import heather_telegram_bot as _bot
            personality_prompt = _bot.get_current_personality_prompt()
        except Exception:
            personality_prompt = "You are Heather, a 42-year-old Uber driver and mom from Kirkland, WA."

        messages = [
            {"role": "system", "content": personality_prompt},
            {"role": "user", "content": rating_prompt}
        ]

        with PerformanceTimer('TEXT_AI', 'image_rating', f"chat_id={chat_id}"):
            response = text_ai_post({
                    "model": config.TEXT_MODEL_NAME,
                    "messages": messages,
                    "temperature": 0.75,
                    "max_tokens": 100,
                    "stream": False
                }, timeout=config.AI_TIMEOUT)

        if response.status_code == 200:
            response_data = response.json()
            rating = response_data['choices'][0]['message']['content'].strip()
            rating = postprocess_response(rating)

            if contains_character_violation(rating):
                return None
            if contains_gender_violation(rating):
                return random.choice(HEATHER_SEXUAL_FALLBACKS)

            return rating
        else:
            stats['text_ai_failures'] += 1
            return None

    except Exception as e:
        stats['text_ai_failures'] += 1
        return None
