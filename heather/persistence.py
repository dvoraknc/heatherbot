"""
heather.persistence — Bot-Runtime JSON I/O
============================================
Schema-versioned, atomic-write persistence for all JSON state files.
Each file is written to a temporary path first, then renamed in place
(``os.replace``) so readers never see a half-written file.

Replaces: heather_telegram_bot.py
  - load_blocked_users / save_blocked_users: lines 1133-1150
  - load_csam_flags / save_csam_flags: lines 1252-1266
  - load_reengagement_history / save_reengagement_history: lines 5859-5887
  - load_ai_disclosure_shown / save_ai_disclosure_shown: lines 5889-5926
  - save_shutdown_timestamp / load_shutdown_timestamp: lines 5928-6028
  - backup_session / restore_session_from_backup: lines 5954-6015
  - load_tip_history / save_tip_history: lines 6030-6058

Dependencies: heather.config (BOT_ROOT, SESSION_NAME), heather.logging_setup (main_logger)
Used by: heather.safety, heather.access_tiers, heather.handlers, heather.admin,
         heather.monitoring, heather.post_response
"""

from __future__ import annotations

import glob as glob_module
import json
import os
import shutil
import time
from datetime import datetime
from typing import Any, Dict, List, Optional, Set

from heather.config import BOT_ROOT, SESSION_NAME
from heather.logging_setup import main_logger


# ── File Paths ────────────────────────────────────────────────────────

BLOCKED_USERS_FILE: str = os.path.join(BOT_ROOT, "blocked_users.json")
CSAM_FLAGS_FILE: str = os.path.join(BOT_ROOT, "csam_flags.json")
REENGAGEMENT_HISTORY_FILE: str = os.path.join(BOT_ROOT, "reengagement_history.json")
AI_DISCLOSURE_FILE: str = os.path.join(BOT_ROOT, "ai_disclosure_shown.json")
CATCHUP_TIMESTAMP_FILE: str = os.path.join(BOT_ROOT, "last_shutdown.json")
TIP_HISTORY_FILE: str = os.path.join(BOT_ROOT, "tip_history.json")
SESSION_BACKUP_DIR: str = os.path.join(BOT_ROOT, "session_backups")
SESSION_BACKUP_MAX_KEEP: int = 5

# Schema versions — bump when file format changes
_SCHEMA_VERSIONS: Dict[str, int] = {
    "blocked_users": 1,
    "csam_flags": 1,
    "reengagement_history": 1,
    "ai_disclosure": 1,
    "shutdown_timestamp": 1,
    "tip_history": 1,
}


# ── Atomic Write Helper ──────────────────────────────────────────────

def _atomic_write_json(
    filepath: str,
    data: Any,
    *,
    indent: Optional[int] = 2,
    ensure_ascii: bool = False,
    label: str = "",
) -> bool:
    """Write JSON data atomically: write to .tmp, then os.replace.

    Args:
        filepath: Destination file path.
        data: JSON-serializable data.
        indent: JSON indentation (None for compact).
        ensure_ascii: Whether to escape non-ASCII characters.
        label: Log label for error messages (e.g., "[TIP]").

    Returns:
        True on success, False on failure.
    """
    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=indent, ensure_ascii=ensure_ascii, default=str)
        os.replace(tmp_path, filepath)
        return True
    except Exception as e:
        main_logger.error(f"{label} Failed to save {os.path.basename(filepath)}: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        return False


def _load_json(
    filepath: str,
    default: Any = None,
    *,
    label: str = "",
) -> Any:
    """Load JSON from file with error handling.

    Args:
        filepath: File to read.
        default: Value to return on missing/corrupt file.
        label: Log label for error messages.

    Returns:
        Parsed JSON data, or *default* if file is missing or corrupt.
    """
    try:
        if os.path.exists(filepath):
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
    except (json.JSONDecodeError, IOError) as e:
        main_logger.warning(f"{label} Failed to load {os.path.basename(filepath)}: {e}")
    return default() if callable(default) else default


# ── Blocked Users ─────────────────────────────────────────────────────

def load_blocked_users() -> Set[int]:
    """Load blocked user IDs from disk.

    Returns:
        Set of blocked chat IDs. Empty set if file is missing.
    """
    data = _load_json(BLOCKED_USERS_FILE, default=dict, label="[BLOCKED]")
    if data and isinstance(data, dict):
        return set(data.get("blocked", []))
    return set()


def save_blocked_users(blocked: Set[int]) -> bool:
    """Persist blocked user set to disk (atomic write).

    Args:
        blocked: Set of chat IDs to block.

    Returns:
        True on success.
    """
    payload = {
        "schema_version": _SCHEMA_VERSIONS["blocked_users"],
        "blocked": sorted(blocked),
    }
    return _atomic_write_json(BLOCKED_USERS_FILE, payload, label="[BLOCKED]")


# ── CSAM Flags ────────────────────────────────────────────────────────

def load_csam_flags() -> List[Dict[str, Any]]:
    """Load CSAM flag entries pending admin review.

    Returns:
        List of flag dicts. Empty list if file is missing.
    """
    data = _load_json(CSAM_FLAGS_FILE, default=list, label="[CSAM]")
    if isinstance(data, list):
        return data
    return []


def save_csam_flags(flags: List[Dict[str, Any]]) -> bool:
    """Persist CSAM flags to disk (atomic write).

    Args:
        flags: List of flag entry dicts.

    Returns:
        True on success.
    """
    return _atomic_write_json(CSAM_FLAGS_FILE, flags, label="[CSAM]")


# ── Re-engagement History ─────────────────────────────────────────────

def load_reengagement_history() -> Dict[str, Any]:
    """Load re-engagement history from JSON file.

    Returns:
        Dict of re-engagement data. Empty dict if missing.
    """
    data = _load_json(REENGAGEMENT_HISTORY_FILE, default=dict, label="[REENGAGEMENT]")
    return data if isinstance(data, dict) else {}


def save_reengagement_history(data: Dict[str, Any]) -> bool:
    """Persist re-engagement history (atomic write).

    Args:
        data: Re-engagement history dict.

    Returns:
        True on success.
    """
    return _atomic_write_json(REENGAGEMENT_HISTORY_FILE, data, label="[REENGAGEMENT]")


# ── AI Disclosure Tracking ────────────────────────────────────────────

def load_ai_disclosure_shown() -> Dict[int, Dict[str, Any]]:
    """Load AI disclosure dict. Migrates old list format automatically.

    Returns:
        Dict mapping chat_id (int) -> disclosure metadata.
    """
    data = _load_json(AI_DISCLOSURE_FILE, default=dict, label="[DISCLOSURE]")
    if isinstance(data, list):
        # Migrate old format: list of IDs -> dict with placeholder metadata
        migrated = {
            int(uid): {"timestamp": None, "source": "unknown", "username": None}
            for uid in data
        }
        main_logger.info(
            f"[DISCLOSURE] Migrated {len(migrated)} users from old list format to dict"
        )
        return migrated
    elif isinstance(data, dict):
        return {int(k): v for k, v in data.items()}
    return {}


def save_ai_disclosure_shown(disclosure_data: Dict[int, Dict[str, Any]]) -> bool:
    """Persist AI disclosure tracking (atomic write).

    Args:
        disclosure_data: Dict mapping chat_id -> disclosure metadata.

    Returns:
        True on success.
    """
    # JSON requires string keys
    serializable = {str(k): v for k, v in disclosure_data.items()}
    return _atomic_write_json(
        AI_DISCLOSURE_FILE, serializable, indent=None, label="[DISCLOSURE]"
    )


# ── Shutdown / Catch-Up Timestamps ───────────────────────────────────

def save_shutdown_timestamp() -> bool:
    """Save current timestamp for crash-recovery catch-up (atomic write).

    Returns:
        True on success.
    """
    now = time.time()
    data = {
        "schema_version": _SCHEMA_VERSIONS["shutdown_timestamp"],
        "timestamp": now,
        "iso": datetime.fromtimestamp(now).isoformat(),
    }
    return _atomic_write_json(CATCHUP_TIMESTAMP_FILE, data, label="[CATCHUP]")


def load_shutdown_timestamp() -> Optional[float]:
    """Load last shutdown/heartbeat timestamp.

    Returns:
        Timestamp as float, or None if missing/corrupt.
    """
    data = _load_json(CATCHUP_TIMESTAMP_FILE, label="[CATCHUP]")
    if data:
        ts = data.get("timestamp")
        if isinstance(ts, (int, float)) and ts > 0:
            return float(ts)
    return None


# ── Tip History ───────────────────────────────────────────────────────

def load_tip_history() -> Dict[str, Any]:
    """Load tip history from JSON file.

    Returns:
        Dict of tip data. Empty dict if missing.
    """
    data = _load_json(TIP_HISTORY_FILE, default=dict, label="[TIP]")
    return data if isinstance(data, dict) else {}


def save_tip_history(
    tipper_status: Dict[int, Dict[str, Any]],
    payment_bot_started_users: Set[int],
) -> bool:
    """Persist tip history (atomic write).

    Args:
        tipper_status: Dict mapping chat_id -> tipper data.
        payment_bot_started_users: Set of users who have started the payment bot.

    Returns:
        True on success.
    """
    data = {str(k): v for k, v in tipper_status.items()}
    data["_started_users"] = sorted(payment_bot_started_users)
    return _atomic_write_json(TIP_HISTORY_FILE, data, label="[TIP]")


# ── Session Backup / Restore ─────────────────────────────────────────

def backup_session(reason: str = "periodic") -> Optional[str]:
    """Back up the Telethon session file with a timestamp.

    Args:
        reason: Backup reason label (e.g., "periodic", "pre-update").

    Returns:
        Path to the backup file, or None on failure.
    """
    session_path = f"{SESSION_NAME}.session"
    if not os.path.exists(session_path):
        main_logger.warning(
            f"[SESSION] Cannot backup — session file not found: {session_path}"
        )
        return None
    try:
        os.makedirs(SESSION_BACKUP_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_name = f"{SESSION_NAME}_{ts}_{reason}.session"
        backup_path = os.path.join(SESSION_BACKUP_DIR, backup_name)
        shutil.copy2(session_path, backup_path)
        main_logger.info(
            f"[SESSION] Backed up session -> {backup_name} "
            f"({os.path.getsize(backup_path)} bytes)"
        )
        # Prune old backups beyond SESSION_BACKUP_MAX_KEEP
        backups = sorted(
            glob_module.glob(
                os.path.join(SESSION_BACKUP_DIR, f"{SESSION_NAME}_*.session")
            )
        )
        while len(backups) > SESSION_BACKUP_MAX_KEEP:
            old = backups.pop(0)
            os.remove(old)
            main_logger.info(f"[SESSION] Pruned old backup: {os.path.basename(old)}")
        return backup_path
    except Exception as e:
        main_logger.error(f"[SESSION] Backup failed: {e}")
        return None


def restore_session_from_backup() -> bool:
    """Restore the most recent healthy session backup.

    Tries backups newest-first, validates SQLite integrity and auth_key
    presence before restoring.

    Returns:
        True if a healthy backup was restored.
    """
    session_path = f"{SESSION_NAME}.session"
    if not os.path.exists(SESSION_BACKUP_DIR):
        main_logger.error("[SESSION] No backup directory found — cannot restore")
        return False
    backups = sorted(
        glob_module.glob(
            os.path.join(SESSION_BACKUP_DIR, f"{SESSION_NAME}_*.session")
        )
    )
    if not backups:
        main_logger.error("[SESSION] No backups available to restore")
        return False
    # Try backups newest-first
    for backup_path in reversed(backups):
        try:
            import sqlite3

            conn = sqlite3.connect(backup_path)
            integrity = conn.execute("PRAGMA integrity_check;").fetchone()
            has_key = conn.execute(
                "SELECT auth_key IS NOT NULL FROM sessions"
            ).fetchone()
            conn.close()
            if integrity[0] != "ok" or not has_key[0]:
                main_logger.warning(
                    f"[SESSION] Backup {os.path.basename(backup_path)} "
                    f"failed integrity check, trying next..."
                )
                continue
            # Backup is healthy — restore it
            if os.path.exists(session_path):
                corrupt_name = (
                    f"{SESSION_NAME}_corrupt_"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.session"
                )
                corrupt_path = os.path.join(SESSION_BACKUP_DIR, corrupt_name)
                shutil.move(session_path, corrupt_path)
                main_logger.info(
                    f"[SESSION] Moved corrupt session -> {corrupt_name}"
                )
            journal_path = f"{SESSION_NAME}.session-journal"
            if os.path.exists(journal_path):
                os.remove(journal_path)
            shutil.copy2(backup_path, session_path)
            main_logger.info(
                f"[SESSION] Restored session from {os.path.basename(backup_path)}"
            )
            return True
        except Exception as e:
            main_logger.warning(
                f"[SESSION] Could not validate backup "
                f"{os.path.basename(backup_path)}: {e}"
            )
            continue
    main_logger.error(
        "[SESSION] All backups failed validation — manual re-auth required"
    )
    return False


# ============================================================================
# Unit test stubs
# ============================================================================
# def test_atomic_write_creates_file(tmp_path):
#     filepath = str(tmp_path / "test.json")
#     assert _atomic_write_json(filepath, {"key": "value"})
#     with open(filepath) as f:
#         data = json.load(f)
#     assert data["key"] == "value"
#
# def test_atomic_write_no_partial_on_error():
#     """If json.dump raises, no .tmp file should remain."""
#     # Would need to mock json.dump to raise
#     pass
#
# def test_load_json_missing_file():
#     data = _load_json("/nonexistent/path.json", default=dict)
#     assert data == {}
#
# def test_load_blocked_users_empty():
#     """Returns empty set when file doesn't exist."""
#     # Would need to mock BLOCKED_USERS_FILE
#     pass
#
# def test_save_load_roundtrip_blocked():
#     """save_blocked_users -> load_blocked_users preserves data."""
#     pass
#
# def test_ai_disclosure_migrates_list():
#     """Old list format [123, 456] should be migrated to dict."""
#     pass
#
# def test_save_tip_history_includes_started_users():
#     """Tip history should include _started_users key."""
#     pass
#
# def test_schema_version_written():
#     """Files that support schema versioning should include the version."""
#     pass
