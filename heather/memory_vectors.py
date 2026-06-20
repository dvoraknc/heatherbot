"""Semantic memory retrieval (#6).

Embeds a user's episodic memories (session summaries, memorable quotes,
personal notes) with Ollama's `nomic-embed-text` model and stores the
768-dim vectors in a stdlib sqlite3 file alongside the JSON profiles.
Retrieval is a pure-Python cosine search over the rows for one user.

100% local. Zero new pip dependencies (only `requests` + stdlib `sqlite3`,
both already present). Every public function fails soft: if Ollama is down
or sqlite errors, callers get None / [] and the hot path is never broken.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Tuple

import requests

logger = logging.getLogger(__name__)

# --- Configuration ---------------------------------------------------------
_OLLAMA_URL = os.getenv("HEATHER_OLLAMA_URL", "http://localhost:11434").rstrip("/")
_EMBED_MODEL = os.getenv("HEATHER_EMBED_MODEL", "nomic-embed-text")
_EMBED_TIMEOUT = 12  # seconds

# DB lives next to the JSON profiles (bot-root/user_profiles/memory_vectors.db)
_DB_PATH = Path(__file__).resolve().parent.parent / "user_profiles" / "memory_vectors.db"

# Guard concurrent writers (embeddings run in an executor thread).
_db_lock = threading.Lock()
_schema_ready = False


# --- Embedding -------------------------------------------------------------
def embed(text: str) -> Optional[List[float]]:
    """Return a 768-dim embedding for `text`, or None on any failure."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        r = requests.post(
            f"{_OLLAMA_URL}/api/embeddings",
            json={"model": _EMBED_MODEL, "prompt": text},
            timeout=_EMBED_TIMEOUT,
        )
        r.raise_for_status()
        vec = r.json().get("embedding")
        if isinstance(vec, list) and vec:
            return [float(x) for x in vec]
    except Exception as e:  # network, json, timeout — all non-fatal
        logger.debug(f"[VECTORS] embed failed: {e}")
    return None


def _cosine(a: List[float], b: List[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


# --- Storage ---------------------------------------------------------------
def _connect() -> sqlite3.Connection:
    global _schema_ready
    _DB_PATH.parent.mkdir(exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH), timeout=10)
    if not _schema_ready:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mem_vectors (
                chat_id    INTEGER NOT NULL,
                mem_key    TEXT    NOT NULL,
                mem_text   TEXT    NOT NULL,
                mem_type   TEXT    NOT NULL,
                text_hash  TEXT    NOT NULL,
                created_at TEXT    NOT NULL,
                embedding  TEXT    NOT NULL,
                PRIMARY KEY (chat_id, mem_key)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_mem_chat ON mem_vectors (chat_id)"
        )
        conn.commit()
        _schema_ready = True
    return conn


def _hash(text: str) -> str:
    return hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:16]


def index_memory(chat_id: int, mem_key: str, mem_text: str, mem_type: str) -> bool:
    """Embed + store one memory. Skips work if the text is unchanged.
    Returns True if a (re)embed happened, False if skipped/failed."""
    mem_text = (mem_text or "").strip()
    if len(mem_text) < 8:
        return False
    h = _hash(mem_text)
    try:
        with _db_lock:
            conn = _connect()
            try:
                row = conn.execute(
                    "SELECT text_hash FROM mem_vectors WHERE chat_id=? AND mem_key=?",
                    (chat_id, mem_key),
                ).fetchone()
                if row and row[0] == h:
                    return False  # already indexed, unchanged
            finally:
                conn.close()
    except Exception as e:
        logger.debug(f"[VECTORS] lookup failed: {e}")
        return False

    vec = embed(mem_text)
    if vec is None:
        return False

    try:
        with _db_lock:
            conn = _connect()
            try:
                conn.execute(
                    """
                    INSERT INTO mem_vectors
                        (chat_id, mem_key, mem_text, mem_type, text_hash, created_at, embedding)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(chat_id, mem_key) DO UPDATE SET
                        mem_text=excluded.mem_text,
                        mem_type=excluded.mem_type,
                        text_hash=excluded.text_hash,
                        created_at=excluded.created_at,
                        embedding=excluded.embedding
                    """,
                    (
                        chat_id,
                        mem_key,
                        mem_text,
                        mem_type,
                        h,
                        datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        json.dumps(vec),
                    ),
                )
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception as e:
        logger.debug(f"[VECTORS] insert failed: {e}")
        return False


def _iter_profile_memories(profile: dict):
    """Yield (mem_key, mem_text, mem_type) for indexable episodic memories."""
    # Session summaries — keyed by date (stable, updates if summary changes).
    for i, mem in enumerate(profile.get("session_memories", []) or []):
        if isinstance(mem, dict):
            summary = (mem.get("summary") or "").strip()
            if summary and "identity is not specified" not in summary.lower():
                date = mem.get("date", f"i{i}")
                # Include content hash so same-day sessions don't collide on key
                # (collisions caused re-embed churn on every index pass).
                yield (f"session:{date}:{_hash(summary)}", summary, "session")

    # Memorable quotes/moments — keyed by content hash.
    for mem in profile.get("memorable", []) or []:
        if isinstance(mem, dict):
            text = (mem.get("text") or "").strip()
        elif isinstance(mem, str):
            text = mem.strip()
        else:
            text = ""
        if text:
            yield (f"memorable:{_hash(text)}", text, "memorable")

    # Personal notes — keyed by content hash.
    for note in profile.get("personal_notes", []) or []:
        if isinstance(note, str) and note.strip():
            yield (f"note:{_hash(note)}", note.strip(), "note")


def index_profile_memories(chat_id: int, profile: dict) -> int:
    """Index all episodic memories for a profile, then prune rows that no
    longer correspond to a current memory (keeps the DB in sync as memories
    roll off the capped lists / get consolidated). Returns count of (re)embeds."""
    count = 0
    current_keys = []
    for mem_key, mem_text, mem_type in _iter_profile_memories(profile):
        current_keys.append(mem_key)
        if index_memory(chat_id, mem_key, mem_text, mem_type):
            count += 1

    # Prune stale rows for this user (only if we actually have a current set).
    if current_keys:
        try:
            with _db_lock:
                conn = _connect()
                try:
                    rows = conn.execute(
                        "SELECT mem_key FROM mem_vectors WHERE chat_id=?", (chat_id,)
                    ).fetchall()
                    stale = [r[0] for r in rows if r[0] not in set(current_keys)]
                    if stale:
                        conn.executemany(
                            "DELETE FROM mem_vectors WHERE chat_id=? AND mem_key=?",
                            [(chat_id, k) for k in stale],
                        )
                        conn.commit()
                        logger.debug(f"[VECTORS] pruned {len(stale)} stale rows for {chat_id}")
                finally:
                    conn.close()
        except Exception as e:
            logger.debug(f"[VECTORS] prune failed: {e}")

    if count:
        logger.info(f"[VECTORS] indexed {count} memories for {chat_id}")
    return count


# --- Retrieval -------------------------------------------------------------
def search(
    chat_id: int, query: str, k: int = 3, min_sim: float = 0.55
) -> List[Tuple[str, str, float]]:
    """Return up to `k` (mem_text, mem_type, similarity) tuples for this user,
    ranked by cosine similarity to `query`, filtered to sim >= min_sim."""
    query = (query or "").strip()
    if not query:
        return []
    qvec = embed(query)
    if qvec is None:
        return []

    try:
        with _db_lock:
            conn = _connect()
            try:
                rows = conn.execute(
                    "SELECT mem_text, mem_type, embedding FROM mem_vectors WHERE chat_id=?",
                    (chat_id,),
                ).fetchall()
            finally:
                conn.close()
    except Exception as e:
        logger.debug(f"[VECTORS] search query failed: {e}")
        return []

    scored: List[Tuple[str, str, float]] = []
    for mem_text, mem_type, emb_json in rows:
        try:
            vec = json.loads(emb_json)
        except Exception:
            continue
        sim = _cosine(qvec, vec)
        if sim >= min_sim:
            scored.append((mem_text, mem_type, sim))

    scored.sort(key=lambda t: t[2], reverse=True)
    return scored[:k]
