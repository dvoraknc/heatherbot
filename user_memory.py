"""
HeatherBot User Memory System
==============================
Per-user persistent profiles that track kinks, personal details, preferences,
conversation history, and session memories. Builds a living profile over time
to personalize chat.

Profiles stored in: user_profiles/{chat_id}.json

Features:
- Kink scoring (14 categories, keyword-based accumulation)
- Personal detail extraction (name, age, location, etc. via regex)
- Session memories (LLM-generated 2-3 sentence summaries per session)
- Memorable moments (standout quotes and revelations)
- Callback prompts (periodic nudges to reference past conversations)
- Heather-shared tracking (what she's told this user, for consistency)
"""

import json, os, re, time, random, logging, requests, yaml
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger("heather_bot")

# ── Kink Persona System ─────────────────────────────────────────────
KINK_PERSONAS_PATH = Path(os.path.dirname(os.path.abspath(__file__))) / "heather_kink_personas.yaml"
_kink_personas: dict = {}

# Map kink scoring categories → persona YAML keys
KINK_TO_PERSONA = {
    "breeding":   "heather_breeding_persona",
    "cnc":        "heather_cnc_persona",
    "domme":      "heather_domme_mommy_persona",
    "anal":       "heather_anal_persona",
    "oral":       "heather_deepthroat_oral_persona",
    "feet":       "heather_body_worship_persona",
    "voyeur":     "heather_voyeur_exhib_persona",
    "cuckold":    "heather_cuckold_persona",
    "bdsm":       "heather_cnc_persona",        # BDSM maps to CNC/rough
    "roleplay":   "heather_gfe_intimate_persona", # Roleplay maps to GFE
    "milf":       "heather_milf_agegap_persona",
    "creampie":   "heather_breeding_persona",    # Creampie maps to breeding
    "dirty_talk": "heather_gfe_intimate_persona", # Dirty talk maps to GFE
    "size":       "heather_bbc_persona",
    # Extended kinks (detected by keyword expansion below)
    "stepfamily":  "heather_stepfamily_persona",
    "uber":        "heather_uber_slut_persona",
    "freeuse":     "heather_freeuse_persona",
    "forced_bi":   "heather_forced_bi_persona",
    "body_worship":"heather_body_worship_persona",
    "findom":      "heather_findom_persona",
    "gangbang":    "heather_gangbang_persona",
}

# Minimum kink score before persona kicks in (discovery phase must happen first)
KINK_PERSONA_THRESHOLD = 3

def _load_kink_personas():
    """Load kink persona definitions from YAML."""
    global _kink_personas
    if _kink_personas:
        return _kink_personas
    try:
        if KINK_PERSONAS_PATH.exists():
            with open(KINK_PERSONAS_PATH, "r", encoding="utf-8") as f:
                _kink_personas = yaml.safe_load(f) or {}
            logger.info(f"Loaded {len(_kink_personas)} kink personas from YAML")
        else:
            logger.warning(f"Kink personas file not found: {KINK_PERSONAS_PATH}")
    except Exception as e:
        logger.error(f"Failed to load kink personas: {e}")
    return _kink_personas

PROFILE_DIR = Path(os.path.dirname(os.path.abspath(__file__))) / "user_profiles"
PROFILE_DIR.mkdir(exist_ok=True)

# Save debounce — don't write to disk on every message
_unsaved_profiles: set = set()  # chat_ids with unsaved changes
SAVE_EVERY_N = 5  # save after this many updates per user
_update_counts: Dict[int, int] = {}

# In-memory cache
_profiles: Dict[int, dict] = {}

# Session tracking for LLM summaries
_session_message_buffer: Dict[int, List[dict]] = {}  # chat_id -> [{role, content, timestamp}]
_last_message_time: Dict[int, float] = {}  # chat_id -> unix timestamp of last message
SESSION_GAP_SECONDS = 7200  # 2 hours = new session

# Callback tracking — don't fire callbacks too often
_last_callback_msg_count: Dict[int, int] = {}
CALLBACK_EVERY_N_MSGS = 18  # suggest a memory callback roughly every 18 messages
CALLBACK_MIN_SESSIONS = 2  # need at least 2 sessions before callbacks kick in

# LLM endpoint for session summaries and extraction
LLM_URL = "http://127.0.0.1:1234/v1/chat/completions"

# LLM-based profile extraction settings
EXTRACTION_INTERVAL = 5   # Run every N user messages
EXTRACTION_TIMEOUT = 25   # Seconds (increased from 15 for peak load)

SUMMARY_SYSTEM_PROMPT = (
    "You are a memory system for a chatbot named Heather. "
    "Given a conversation excerpt, write a brief session summary (2-3 sentences max). "
    "ALWAYS include: (1) specific personal details the user shared (name, age, job, relationship status, location — use exact numbers/names), "
    "(2) what sexual themes or kinks came up (be specific: breeding, anal, feet, roleplay, etc.), "
    "(3) emotional tone and any standout quotes. "
    "Write in third person about the user (e.g., 'He is 34, works as a mechanic, talked about...'). "
    "Be specific and factual. Include numbers, names, and details — not vague summaries."
)

# ── Kink Categories ──────────────────────────────────────────────────
KINK_KEYWORDS = {
    "breeding": [
        "breed", "breeding", "pregnant", "impregnate", "knock up", "knocked up",
        "put a baby", "cum inside", "fill me", "seed", "womb", "fertility",
        "breed me", "bred", "make me pregnant", "baby batter",
    ],
    "cnc": [
        "cnc", "overpower", "force", "pin me down", "pin you down", "hold me down",
        "against my will", "take me", "struggle", "resist", "no choice",
        "make me", "fight back", "rough", "forceful",
    ],
    "domme": [
        "mommy", "mistress", "dominate", "humiliate", "pathetic", "small cock",
        "small dick", "tiny cock", "worthless", "punish", "sissy", "femdom",
        "step on", "spit on", "chastity", "beg", "degradation",
    ],
    "anal": [
        "anal", "ass fuck", "in the ass", "backdoor", "butt fuck",
        "ass to mouth", "atm", "in my ass", "up the ass", "tight ass",
    ],
    "oral": [
        "blowjob", "blow job", "suck", "deepthroat", "deep throat", "throat",
        "face fuck", "gag", "swallow", "mouth", "head", "bj",
    ],
    "feet": [
        "feet", "foot", "toes", "soles", "foot job", "footjob",
        "lick my feet", "worship my feet", "foot fetish",
    ],
    "voyeur": [
        "watch", "watching", "caught", "spy", "peeping", "hidden camera",
        "see you", "show me", "let me watch", "exhibitionist",
    ],
    "cuckold": [
        "cuck", "cuckold", "share", "watch me", "another guy", "bull",
        "hotwife", "wife sharing", "sloppy seconds", "other men",
    ],
    "bdsm": [
        "tie me", "tied up", "handcuffs", "blindfold", "collar", "leash",
        "whip", "spank", "paddle", "bondage", "rope", "restrain",
    ],
    "roleplay": [
        "roleplay", "role play", "pretend", "fantasy", "scenario",
        "let's play", "be my", "act like", "dress up",
    ],
    "milf": [
        "milf", "older woman", "mature", "cougar", "experienced",
        "mom", "mommy", "older", "age gap",
    ],
    "creampie": [
        "creampie", "cream pie", "cum in", "fill up", "load inside",
        "don't pull out", "cum deep", "finish inside",
    ],
    "dirty_talk": [
        "talk dirty", "dirty talk", "tell me", "say something",
        "describe", "what would you do", "tell me what",
    ],
    "size": [
        "big cock", "huge cock", "bbc", "big dick", "hung", "monster",
        "stretch", "split me", "can you take it", "too big",
    ],
    "stepfamily": [
        "stepson", "stepmom", "step mom", "step son", "stepdad", "step family",
        "tyler", "erick's son", "taboo family", "forbidden family", "not my real",
    ],
    "uber": [
        "uber", "lyft", "rideshare", "driver", "backseat", "passenger",
        "ride", "pick me up", "jen dvorak", "uber slut", "cum tip",
    ],
    "freeuse": [
        "freeuse", "free use", "use me anytime", "always available",
        "any hole anytime", "no questions", "just use me", "walking cumdump",
    ],
    "forced_bi": [
        "forced bi", "bi cuck", "suck the bull", "fluff", "make him suck",
        "cuck sucks", "bi curious", "pegging", "strap on", "strapon",
    ],
    "gangbang": [
        "gangbang", "gang bang", "group", "train", "run a train",
        "how many guys", "multiple", "airtight", "all holes",
    ],
    "findom": [
        "pay", "tribute", "cash", "findom", "money", "buy", "tip me",
        "spoil", "wallet", "sugar", "allowance",
    ],
    "body_worship": [
        "worship", "labia", "lips", "nipples", "tits worship",
        "ass worship", "rimming", "eat me", "face sitting", "facesit",
    ],
}

# ── Personal Detail Patterns ─────────────────────────────────────────
PERSONAL_PATTERNS = {
    "name": [
        re.compile(r"(?:my name(?:'s| is)|i'm|im|call me|i am)\s+([A-Z][a-z]{1,15})\b", re.I),
        re.compile(r"^([A-Z][a-z]{2,12})(?:\s+here|\s+btw)$", re.I),
    ],
    "age": [
        re.compile(r"(?:i'm|im|i am)\s+(\d{2})\b", re.I),
        re.compile(r"(\d{2})\s*(?:years?\s*old|yo|yr|y/o)\b", re.I),
    ],
    "location": [
        re.compile(r"(?:i'm|im|i am|i live|living|located|based)\s+(?:in|from|near)\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)?)", re.I),
        re.compile(r"(?:from|in)\s+((?:New York|Los Angeles|San Francisco|Chicago|Houston|Phoenix|Dallas|Austin|Seattle|Portland|Denver|Atlanta|Miami|Boston|Detroit|Minneapolis|San Diego|Tampa|Orlando|Nashville|Charlotte|San Antonio|Columbus|Indianapolis|Jacksonville|Fort Worth|Memphis|Baltimore|Milwaukee|Albuquerque|Tucson|Sacramento|Kansas City|Las Vegas|Long Beach|Mesa|Virginia Beach|Raleigh|Omaha|Colorado Springs|Oakland|Minneapolis|Cleveland|Tulsa|Arlington|New Orleans|Bakersfield|Honolulu|St Louis|Pittsburgh|Anchorage|Henderson|Lexington|Stockton|Cincinnati|St Paul|Greensboro|Lincoln|Buffalo|Plano|Chandler|Norfolk|Madison|Lubbock|Irvine|Winston-Salem|Glendale|Garland|Hialeah|Laredo|Jersey City|Scottsdale|Baton Rouge|North Las Vegas|Gilbert|Reno|Chesapeake|Richmond|Spokane|Fremont|Boise|Montgomery|Tacoma|Modesto|Fayetteville))\b", re.I),
    ],
    "relationship": [
        re.compile(r"\b(married|single|divorced|separated|widowed|engaged|girlfriend|wife|gf)\b", re.I),
    ],
    "cock_size": [
        re.compile(r"(\d+(?:\.\d+)?)\s*(?:inch|in|\")\s*(?:cock|dick|penis)?", re.I),
        re.compile(r"(?:cock|dick|penis)\s+(?:is\s+)?(\d+(?:\.\d+)?)\s*(?:inch|in|\")?", re.I),
    ],
    "cock_desc": [
        re.compile(r"(?:my (?:cock|dick) is|i have a|got a)\s+((?:thick|thin|curved|straight|fat|long|uncut|circumcised|pierced|veiny)(?:\s+(?:and|&)\s+(?:thick|thin|curved|straight|fat|long|uncut|circumcised|pierced|veiny))?)", re.I),
    ],
}

# ── Memorable Moment Patterns ────────────────────────────────────────
# Detect messages worth saving as "memorable" — personal revelations, emotional moments
MEMORABLE_PATTERNS = [
    # Personal revelations
    re.compile(r"(?:my wife|my gf|my girlfriend|my husband)\s+(?:doesn't|doesn't|doesn't know|found out|left me|cheated|divorced)", re.I),
    re.compile(r"(?:i just|i recently)\s+(?:got divorced|got separated|broke up|lost my|got fired|got promoted)", re.I),
    re.compile(r"(?:it's|its|today is|tomorrow is)\s+my\s+birthday", re.I),
    re.compile(r"(?:i'm|im|i am)\s+(?:going through|dealing with|struggling with)\s+", re.I),
    re.compile(r"(?:i've never|i never)\s+(?:told anyone|shared this|done this before)", re.I),
    re.compile(r"you're the (?:only one|first person|best thing)", re.I),
    # Strong emotional signals
    re.compile(r"(?:i think i'm|i'm falling|i might be)\s+(?:in love|falling for|catching feelings)", re.I),
    re.compile(r"(?:this is|you are|that was)\s+the (?:best|hottest|most amazing|most incredible)", re.I),
    re.compile(r"(?:i can't stop|can't quit)\s+(?:thinking about|coming back)", re.I),
    re.compile(r"you (?:make me|made me)\s+(?:feel|cum|laugh|smile|happy)", re.I),
    # Specific scenario requests worth remembering
    re.compile(r"(?:can we|let's|i want to)\s+(?:roleplay|pretend|do that again|try)", re.I),
    re.compile(r"(?:remember when|last time)\s+(?:we|you|i)", re.I),
]

# ── Standout Quote Detection ─────────────────────────────────────────
# Messages that are substantial enough to save as quotes (not just "yeah" or "mmm")
MIN_QUOTE_LENGTH = 40  # characters
MAX_QUOTES_PER_SESSION = 3
MAX_STORED_QUOTES = 15


def _empty_profile() -> dict:
    """Create a blank profile template."""
    return {
        "name": None,
        "age": None,
        "location": None,
        "relationship": None,
        "cock": {"size": None, "description": None},
        "kinks": {k: 0 for k in KINK_KEYWORDS},
        "turn_ons": [],          # top kinks sorted by score (computed on read)
        "personal_notes": [],    # things they've shared (capped at 20)
        "heather_shared": [],    # things Heather told them (capped at 15)
        "memorable": [],         # standout moments/quotes (capped at MAX_STORED_QUOTES)
        "session_memories": [],  # LLM-generated session summaries (capped at 20)
        "sessions": 0,
        "total_msgs": 0,
        "first_seen": None,
        "last_seen": None,
        "last_session_date": None,
    }


def load_profile(chat_id: int) -> dict:
    """Load a user profile from disk or cache."""
    if chat_id in _profiles:
        return _profiles[chat_id]

    profile_path = PROFILE_DIR / f"{chat_id}.json"
    if profile_path.exists():
        try:
            with open(profile_path, "r", encoding="utf-8") as f:
                profile = json.load(f)
            # Merge with template to add any new fields
            template = _empty_profile()
            for key, default in template.items():
                if key not in profile:
                    profile[key] = default
            if "kinks" in profile:
                for kink in template["kinks"]:
                    if kink not in profile["kinks"]:
                        profile["kinks"][kink] = 0
            _profiles[chat_id] = profile
            return profile
        except (json.JSONDecodeError, IOError):
            pass

    profile = _empty_profile()
    profile["first_seen"] = datetime.now().strftime("%Y-%m-%d")
    _profiles[chat_id] = profile
    return profile


def save_profile(chat_id: int, force: bool = False):
    """Save a user profile to disk (with debounce)."""
    if chat_id not in _profiles:
        return

    _unsaved_profiles.add(chat_id)
    _update_counts[chat_id] = _update_counts.get(chat_id, 0) + 1

    if force or _update_counts.get(chat_id, 0) >= SAVE_EVERY_N:
        _flush_profile(chat_id)


def _flush_profile(chat_id: int):
    """Actually write profile to disk."""
    if chat_id not in _profiles:
        return
    profile_path = PROFILE_DIR / f"{chat_id}.json"
    try:
        with open(profile_path, "w", encoding="utf-8") as f:
            json.dump(_profiles[chat_id], f, ensure_ascii=False, indent=2)
        _unsaved_profiles.discard(chat_id)
        _update_counts[chat_id] = 0
    except IOError:
        pass


def save_all():
    """Flush all unsaved profiles to disk (call on shutdown).
    Also generates session summaries for any active sessions."""
    # Generate summaries for any active sessions before flushing
    for chat_id in list(_session_message_buffer.keys()):
        buf = _session_message_buffer[chat_id]
        if len(buf) >= 6:  # only summarize meaningful sessions
            _generate_and_store_summary(chat_id, buf)
    _session_message_buffer.clear()

    # Flush all profiles
    for chat_id in list(_unsaved_profiles):
        _flush_profile(chat_id)
    # Also flush any profiles that have session summaries just generated
    for chat_id in list(_profiles.keys()):
        if chat_id in _profiles:
            _flush_profile(chat_id)


# ── Session Message Buffer ───────────────────────────────────────────

def _buffer_message(chat_id: int, role: str, content: str):
    """Add a message to the session buffer. Detect session gaps and trigger summaries."""
    now = time.time()

    # Check if this is a new session (gap > 2 hours since last message)
    if chat_id in _last_message_time:
        gap = now - _last_message_time[chat_id]
        if gap > SESSION_GAP_SECONDS:
            # Session ended — summarize the old buffer
            old_buffer = _session_message_buffer.get(chat_id, [])
            if len(old_buffer) >= 6:  # need enough messages for a meaningful summary
                _generate_and_store_summary(chat_id, old_buffer)
            # Clear buffer for new session
            _session_message_buffer[chat_id] = []

    _last_message_time[chat_id] = now

    if chat_id not in _session_message_buffer:
        _session_message_buffer[chat_id] = []

    _session_message_buffer[chat_id].append({
        "role": role,
        "content": content,
        "timestamp": now,
    })

    # Cap buffer at 40 messages (keep most recent)
    if len(_session_message_buffer[chat_id]) > 40:
        _session_message_buffer[chat_id] = _session_message_buffer[chat_id][-40:]


def _generate_and_store_summary(chat_id: int, messages: List[dict]):
    """Call LLM to generate a session summary and store it in the profile."""
    profile = load_profile(chat_id)

    # Build transcript from buffer
    transcript_lines = []
    for msg in messages[-24:]:  # last 24 messages max to keep prompt small
        speaker = "User" if msg["role"] == "user" else "Heather"
        # Truncate very long messages
        content = msg["content"][:200] if len(msg["content"]) > 200 else msg["content"]
        transcript_lines.append(f"{speaker}: {content}")
    transcript = "\n".join(transcript_lines)

    payload = {
        "model": "local-model",
        "messages": [
            {"role": "system", "content": SUMMARY_SYSTEM_PROMPT},
            {"role": "user", "content": f"Summarize this conversation for future reference:\n\n{transcript}"},
        ],
        "temperature": 0.3,
        "max_tokens": 150,
        "stream": False,
    }

    try:
        resp = requests.post(LLM_URL, json=payload, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            summary = data["choices"][0]["message"]["content"].strip()

            # Get session date from first message timestamp
            session_date = datetime.fromtimestamp(messages[0]["timestamp"]).strftime("%Y-%m-%d")

            session_entry = {
                "date": session_date,
                "summary": summary,
                "msg_count": len(messages),
            }

            if "session_memories" not in profile:
                profile["session_memories"] = []
            profile["session_memories"].append(session_entry)

            # Cap at 20 session memories (oldest roll off)
            if len(profile["session_memories"]) > 20:
                profile["session_memories"] = profile["session_memories"][-20:]

            save_profile(chat_id, force=True)
            logger.info(f"[MEMORY] Generated session summary for {chat_id}: {summary[:80]}...")
        else:
            logger.warning(f"[MEMORY] LLM returned {resp.status_code} for session summary of {chat_id}")
    except Exception as e:
        logger.warning(f"[MEMORY] Failed to generate session summary for {chat_id}: {e}")


# ── Core Update Functions ────────────────────────────────────────────

def update_from_user_message(chat_id: int, message: str, display_name: str = None):
    """Extract info from a user message and update their profile."""
    profile = load_profile(chat_id)
    msg_lower = message.lower()
    changed = False

    # -- Buffer message for session summary --
    _buffer_message(chat_id, "user", message)

    # -- Update session tracking --
    today = datetime.now().strftime("%Y-%m-%d")
    if profile["last_session_date"] != today:
        profile["sessions"] += 1
        profile["last_session_date"] = today
        changed = True
    profile["total_msgs"] += 1
    profile["last_seen"] = today
    if not profile["first_seen"]:
        profile["first_seen"] = today

    # -- Detect cross-platform source --
    platform_mentions = {
        'twitter': ['twitter', ' x ', 'your x ', 'on x', 'saw on x', 'from x', '@uberslutty'],
        'discord': ['discord', 'your discord', 'from discord', 'on discord', 'your stories'],
        'reddit': ['reddit', 'from reddit', 'your husband', 'frank sent', 'talked to frank', 'saw your post'],
        'fetlife': ['fetlife', 'fet life', 'from fetlife'],
    }
    for platform, keywords in platform_mentions.items():
        if any(kw in msg_lower for kw in keywords):
            existing_facts = profile.get("personal_facts", [])
            platform_fact = f"came from {platform}"
            if not any(platform in str(f).lower() for f in existing_facts):
                existing_facts.append(platform_fact)
                profile["personal_facts"] = existing_facts
                changed = True
                logger.info(f"[MEMORY] Detected {platform} source for {chat_id}")

    # -- Score kinks from message content --
    for kink, keywords in KINK_KEYWORDS.items():
        hits = sum(1 for kw in keywords if kw in msg_lower)
        if hits > 0:
            profile["kinks"][kink] = profile["kinks"].get(kink, 0) + hits
            changed = True

    # -- Extract personal details --
    for field, patterns in PERSONAL_PATTERNS.items():
        for pat in patterns:
            m = pat.search(message)
            if m:
                value = m.group(1).strip()
                if field == "name" and not profile["name"]:
                    skip = {"heather", "babe", "baby", "sexy", "hey", "hi",
                            "the", "just", "not", "yes", "yeah"}
                    if value.lower() not in skip and len(value) > 1:
                        profile["name"] = value
                        changed = True
                elif field == "age" and not profile["age"]:
                    age_val = int(value)
                    if 18 <= age_val <= 80:
                        profile["age"] = str(age_val)
                        changed = True
                elif field == "location" and not profile["location"]:
                    profile["location"] = value
                    changed = True
                elif field == "relationship":
                    profile["relationship"] = value.lower()
                    changed = True
                elif field == "cock_size":
                    size = float(value)
                    if 3 <= size <= 14:
                        profile["cock"]["size"] = f"{value} inches"
                        changed = True
                elif field == "cock_desc":
                    profile["cock"]["description"] = value.lower()
                    changed = True

    # -- Capture notable personal details (freeform) --
    personal_triggers = [
        (r"i (?:work|am) (?:a |an |in )(.{5,40}?)(?:\.|,|!|\?|$)", "works as/in"),
        (r"my (?:wife|gf|girlfriend) (.{5,50}?)(?:\.|,|!|\?|$)", "partner"),
        (r"i (?:have|got) (?:a |)(\d+ (?:kid|child|son|daughter))", "kids"),
        (r"i(?:'m| am) (?:a |)(veteran|military|army|navy|marine|air force)", "military"),
    ]
    for pat_str, label in personal_triggers:
        m = re.search(pat_str, message, re.I)
        if m:
            note = f"{label}: {m.group(1).strip()}"
            if note not in profile["personal_notes"] and len(profile["personal_notes"]) < 20:
                profile["personal_notes"].append(note)
                changed = True

    # -- Detect memorable moments --
    if len(message) >= MIN_QUOTE_LENGTH:
        for pat in MEMORABLE_PATTERNS:
            if pat.search(message):
                _store_memorable(chat_id, profile, message)
                changed = True
                break  # one match is enough

    # -- Store standout quotes (long, substantive user messages) --
    if len(message) >= 60 and not message.startswith("/"):
        # Score message interestingness (personal disclosure, emotion, detail)
        interest_score = 0
        interest_keywords = [
            "i feel", "i think", "i want", "i need", "i love", "i miss",
            "i remember", "i wish", "honestly", "truth is", "confession",
            "secret", "never told", "first time", "always wanted",
            "my wife", "my gf", "my girlfriend", "my husband", "my ex",
            "work", "job", "boss", "kids", "son", "daughter",
        ]
        for kw in interest_keywords:
            if kw in msg_lower:
                interest_score += 1
        if interest_score >= 2:
            _store_memorable(chat_id, profile, message, label="quote")
            changed = True

    if changed:
        save_profile(chat_id)


def _store_memorable(chat_id: int, profile: dict, message: str, label: str = "moment"):
    """Store a memorable moment/quote in the profile."""
    if "memorable" not in profile:
        profile["memorable"] = []

    # Don't store duplicates or near-duplicates
    msg_snippet = message[:100].strip()
    for existing in profile["memorable"]:
        if isinstance(existing, dict):
            if existing.get("text", "")[:80] == msg_snippet[:80]:
                return
        elif isinstance(existing, str):
            if existing[:80] == msg_snippet[:80]:
                return

    entry = {
        "text": msg_snippet,
        "date": datetime.now().strftime("%Y-%m-%d"),
        "type": label,
    }
    profile["memorable"].append(entry)

    # Cap at MAX_STORED_QUOTES
    if len(profile["memorable"]) > MAX_STORED_QUOTES:
        profile["memorable"] = profile["memorable"][-MAX_STORED_QUOTES:]


def update_from_bot_reply(chat_id: int, reply: str):
    """Track what Heather has shared with this user (for consistency)."""
    profile = load_profile(chat_id)
    reply_lower = reply.lower()

    # Buffer Heather's reply for session summary
    _buffer_message(chat_id, "assistant", reply)

    # Track key revelations Heather makes
    shared_triggers = [
        ("uber", "uber driving stories"),
        ("erick", "late husband Erick"),
        ("navy", "navy service"),
        ("emma", "daughter Emma"),
        ("jake", "son Jake"),
        ("evan", "son Evan"),
        ("frank", "boyfriend Frank"),
        ("kirkland", "lives in Kirkland"),
    ]
    for keyword, label in shared_triggers:
        if keyword in reply_lower:
            if label not in profile["heather_shared"] and len(profile["heather_shared"]) < 15:
                profile["heather_shared"].append(label)
                save_profile(chat_id)


# ── Query Functions ──────────────────────────────────────────────────

def get_active_persona(chat_id: int) -> dict | None:
    """Get the active kink persona for a user.

    Returns dict with persona_key, kink_name, score, or None if not set.
    """
    profile = load_profile(chat_id)
    persona_key = profile.get("active_persona")
    if not persona_key:
        return None
    return {
        "persona_key": persona_key,
        "kink": profile.get("active_persona_kink", ""),
        "score": profile.get("active_persona_score", 0),
    }


def get_all_persona_assignments() -> dict:
    """Scan all user profiles and return persona distribution.

    Returns dict like:
        {"heather_breeding_persona": [chat_id1, chat_id2, ...], ...}
    """
    distribution: Dict[str, list] = {}
    if not PROFILE_DIR.exists():
        return distribution

    for profile_file in PROFILE_DIR.glob("*.json"):
        try:
            with open(profile_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            persona = data.get("active_persona")
            if persona:
                chat_id = int(profile_file.stem)
                distribution.setdefault(persona, []).append(chat_id)
        except Exception:
            continue

    return distribution


def get_top_kinks(chat_id: int, n: int = 5) -> list:
    """Get the user's top N kinks by score."""
    profile = load_profile(chat_id)
    kinks = profile.get("kinks", {})
    sorted_kinks = sorted(kinks.items(), key=lambda x: x[1], reverse=True)
    return [(k, v) for k, v in sorted_kinks[:n] if v > 0]


def build_kink_persona_prompt(chat_id: int, total_msgs: int = 0) -> str:
    """Build a kink-specific persona injection based on the user's top kink.

    Returns empty string if:
    - User has no strong kink detected yet (below threshold)
    - Too early in conversation (discovery phase)
    - No matching persona found

    The injection tells Heather to LEAN INTO the user's primary kink hard,
    using specific language, scenarios, and behaviors from the persona YAML.
    """
    personas = _load_kink_personas()
    if not personas:
        return ""

    top_kinks = get_top_kinks(chat_id, 1)
    if not top_kinks:
        return ""

    top_kink, score = top_kinks[0]

    # Don't inject until kink is clearly established
    if score < KINK_PERSONA_THRESHOLD:
        return ""

    # Get the matching persona
    persona_key = KINK_TO_PERSONA.get(top_kink)
    if not persona_key or persona_key not in personas:
        return ""

    persona = personas[persona_key]

    # Determine conversation phase based on message count
    profile = load_profile(chat_id)
    msgs = total_msgs or profile.get("total_msgs", 0)

    if msgs < 4:
        # Too early — warmth phase, no persona yet
        return ""

    # Store active persona in user profile for tracking/querying
    prev_persona = profile.get("active_persona")
    if prev_persona != persona_key:
        profile["active_persona"] = persona_key
        profile["active_persona_kink"] = top_kink
        profile["active_persona_score"] = score
        save_profile(chat_id, force=True)
        if prev_persona:
            logger.info(f"[KINK] User {chat_id} persona changed: {prev_persona} → {persona_key} (score: {score})")
        else:
            logger.info(f"[KINK] User {chat_id} persona assigned: {persona_key} (kink: {top_kink}, score: {score})")

    if msgs < 10:
        # Discovery/early escalation — light persona hints
        return _build_light_persona(top_kink, persona)
    else:
        # Full escalation — heavy persona injection
        return _build_full_persona(top_kink, persona)


def _build_light_persona(kink_name: str, persona: dict) -> str:
    """Light persona hint for early conversations (msgs 4-10)."""
    # Pull 2-3 verbal responses for flavor
    verbals = []
    for key in ["verbal_responses", "verbal_encouragement", "verbal_humiliation"]:
        section = persona.get(key, {})
        if isinstance(section, dict):
            for sub_key, phrases in section.items():
                if isinstance(phrases, list):
                    verbals.extend(phrases[:2])
        elif isinstance(section, list):
            verbals.extend(section[:2])

    sample = random.sample(verbals, min(2, len(verbals))) if verbals else []
    sample_str = " | ".join(f'"{s}"' for s in sample)

    return (
        f"\n\n[KINK FOCUS: {kink_name.upper()}]\n"
        f"This user is showing strong interest in {kink_name}. "
        f"Start leaning into this — drop hints, tease with scenarios, probe deeper. "
        f"Ask what specifically about {kink_name} gets them going. "
        f"Sample energy: {sample_str}\n"
    )


def _build_full_persona(kink_name: str, persona: dict) -> str:
    """Full persona injection for established conversations (msgs 10+)."""
    parts = [f"\n\n[KINK PERSONA: {kink_name.upper()} — GO HARD]"]

    # Core traits
    core = persona.get("core_traits", {})
    if core:
        trait_strs = []
        for k, v in core.items():
            if isinstance(v, str):
                trait_strs.append(f"{k}: {v}")
            elif isinstance(v, list):
                trait_strs.append(f"{k}: {', '.join(str(i) for i in v[:3])}")
        if trait_strs:
            parts.append("Core: " + " | ".join(trait_strs[:5]))

    # Verbal responses — pull the best lines
    all_verbals = []
    for key, section in persona.items():
        if "verbal" in key.lower() or "responses" in key.lower():
            if isinstance(section, dict):
                for sub_key, phrases in section.items():
                    if isinstance(phrases, list):
                        all_verbals.extend(phrases)
            elif isinstance(section, list):
                all_verbals.extend(section)

    if all_verbals:
        samples = random.sample(all_verbals, min(5, len(all_verbals)))
        parts.append("USE phrases like: " + " | ".join(f'"{s}"' for s in samples))

    # Cuckold integration
    cuck = persona.get("cuckold_integration", {})
    if cuck:
        cuck_strs = [f"{k}: {v}" for k, v in cuck.items() if isinstance(v, str)]
        if cuck_strs:
            parts.append("Frank's role: " + " | ".join(cuck_strs[:3]))

    # Session flow
    flow = persona.get("session_flow", {})
    if flow:
        seq = flow.get("sequence", [])
        if isinstance(seq, list) and seq:
            if isinstance(seq[0], str):
                parts.append("Flow: " + " → ".join(seq[:5]))
            elif isinstance(seq[0], dict):
                flow_strs = []
                for step in seq[:5]:
                    for k, v in step.items():
                        flow_strs.append(f"{k}: {v}")
                parts.append("Flow: " + " → ".join(flow_strs))

    parts.append(
        f"DOUBLE DOWN on {kink_name} — this is what gets this user off. "
        f"Every response should drip with {kink_name} energy. "
        f"Be the filthiest, most depraved version of yourself for this kink. "
        f"You're a proud slut who LOVES this."
    )

    return "\n".join(parts) + "\n"


def _build_history_recall(profile: dict) -> str:
    """Build a natural memory hook from past session data.

    Picks the most interesting detail from the user's history and frames it
    as something Heather would naturally bring up — like a friend who remembers.
    """
    recalls = []

    # Recall from session memories (most valuable)
    session_mems = profile.get("session_memories", [])
    if session_mems:
        # Pick a random past session (not the most recent — that's too obvious)
        older_mems = session_mems[:-1] if len(session_mems) > 1 else session_mems
        if older_mems:
            mem = random.choice(older_mems)
            if isinstance(mem, dict):
                summary = mem.get("summary", "")
                if summary:
                    recalls.append(f"RECALL from a past chat: {summary}")

    # Recall from memorable quotes
    memorables = profile.get("memorable", [])
    if memorables and len(memorables) > 1:
        mem = random.choice(memorables[:-1])  # Not the latest
        text = mem.get("text", mem) if isinstance(mem, dict) else str(mem)
        if text and len(text) > 15:
            recalls.append(f"He once said: \"{text[:100]}\" — reference this naturally if it fits.")

    # Recall from personal notes
    notes = profile.get("personal_notes", [])
    if notes:
        note = random.choice(notes)
        recalls.append(f"You know about him: {note}. Ask a follow-up about this.")

    # Recall from kink history
    kinks = profile.get("kinks", {})
    top_kinks = sorted(kinks.items(), key=lambda x: x[1], reverse=True)[:2]
    if top_kinks and top_kinks[0][1] >= 5:
        kink_name = top_kinks[0][0]
        recalls.append(f"He's really into {kink_name} — bring it up like you remember: 'still thinking about that {kink_name} stuff you told me about 😏'")

    # Recall from name/location
    if profile.get("name") and profile.get("location"):
        recalls.append(f"You know his name is {profile['name']} and he's from {profile['location']}. Use his name naturally.")

    if not recalls:
        return ""

    # Pick 1-2 recalls to keep prompt lean
    selected = random.sample(recalls, min(2, len(recalls)))
    return "DEEP RECALL: " + " | ".join(selected)


def _should_inject_callback(chat_id: int) -> bool:
    """Decide if we should nudge Heather to reference a past conversation."""
    profile = load_profile(chat_id)

    # Need enough history for callbacks to make sense
    sessions = profile.get("sessions", 0)
    if sessions < CALLBACK_MIN_SESSIONS:
        return False

    # Need session memories or memorable moments to reference
    has_memories = bool(profile.get("session_memories")) or bool(profile.get("memorable"))
    if not has_memories:
        return False

    # Check message count cooldown
    total = profile.get("total_msgs", 0)
    last_callback = _last_callback_msg_count.get(chat_id, 0)
    if total - last_callback < CALLBACK_EVERY_N_MSGS:
        return False

    # 35% chance when eligible (not every time)
    return random.random() < 0.35


def _build_callback_prompt(chat_id: int) -> str:
    """Build a callback nudge referencing a past memory."""
    profile = load_profile(chat_id)
    candidates = []

    # Session memories as candidates
    for mem in profile.get("session_memories", []):
        if isinstance(mem, dict):
            candidates.append(f"({mem.get('date', '?')}): {mem.get('summary', '')}")

    # Memorable moments as candidates
    for mem in profile.get("memorable", []):
        if isinstance(mem, dict):
            candidates.append(f"({mem.get('date', '?')}): He said: \"{mem.get('text', '')}\"")
        elif isinstance(mem, str):
            candidates.append(f"He once said: \"{mem}\"")

    if not candidates:
        return ""

    # Pick 1-2 random memories to reference
    selected = random.sample(candidates, min(2, len(candidates)))
    memory_text = "\n".join(f"  - {m}" for m in selected)

    # Track that we fired a callback
    _last_callback_msg_count[chat_id] = profile.get("total_msgs", 0)

    return (
        f"\n\n[MEMORY CALLBACK: You remember past conversations with this person. "
        f"Here are things you recall:\n{memory_text}\n"
        f"Naturally reference one of these if it fits — ask how something turned out, "
        f"mention you were thinking about something they said, or callback to a shared moment. "
        f"Be casual and natural, not forced. Only reference it if it flows with the current conversation.]"
    )


# ── LLM-Based Profile Extraction ────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = (
    "You are a profile extraction system. Given a conversation between a user and a chatbot named Heather, "
    "extract factual details about the USER (not Heather). Return ONLY valid JSON.\n\n"
    '{"name": null, "age": null, "location": null, "relationship_status": null, '
    '"occupation": null, "physical_description": null, '
    '"interests": [], "sexual_preferences": [], "personal_facts": [], '
    '"emotional_state": null, "relationship_with_heather": null}\n\n'
    "CRITICAL RULES:\n"
    "- Only extract details the USER explicitly stated about THEMSELVES\n"
    "- name: ONLY if they clearly introduced themselves (e.g. 'I'm Mike', 'call me Dave', 'my name is John'). "
    "Do NOT extract random words, verbs, adjectives, body parts, medical terms, acronyms, or words from sexual context as names. "
    "A name must be a proper first name like Mike, Dave, John, Sarah — NOT a common English word. "
    "If unsure, ALWAYS use null. It is MUCH better to return null than to guess wrong.\n"
    "- location: ONLY if they said where they live/are from (e.g. 'I'm in Seattle', 'from Texas'). "
    "Do NOT extract body parts or sexual terms as locations\n"
    "- age: ONLY explicit numbers (e.g. 'I'm 35'). Must be 18-99\n"
    "- sexual_preferences: kinks, fantasies, turn-ons the USER expressed wanting to do\n"
    "- personal_facts: real life details — job, family, hobbies, life events\n"
    "- Do NOT extract anything Heather said about herself as the user's details\n"
    "- Use null for unknown fields, empty lists for no items\n"
    "- Return ONLY the JSON object, no explanation or markdown"
)


def extract_profile_with_llm(chat_id: int, recent_messages: list) -> Optional[dict]:
    """Call Dolphin LLM to extract structured profile data from recent conversation.

    Args:
        chat_id: User chat ID (for logging)
        recent_messages: List of message dicts [{"role": "user"/"assistant", "content": "..."}]

    Returns:
        Dict with extracted profile fields, or None on failure.
    """
    if not recent_messages:
        return None

    # Build conversation text from last 10 messages
    last_msgs = recent_messages[-10:]
    transcript_lines = []
    for msg in last_msgs:
        role = msg.get("role", "user")
        speaker = "User" if role == "user" else "Heather"
        content = msg.get("content", "")
        if content:
            # Truncate very long messages
            content = content[:300] if len(content) > 300 else content
            transcript_lines.append(f"{speaker}: {content}")

    if not transcript_lines:
        return None

    transcript = "\n".join(transcript_lines)

    payload = {
        "model": "local-model",
        "messages": [
            {"role": "system", "content": EXTRACTION_SYSTEM_PROMPT},
            {"role": "user", "content": f"Extract user profile details from this conversation:\n\n{transcript}"},
        ],
        "temperature": 0.1,
        "max_tokens": 500,
        "stream": False,
    }

    try:
        resp = requests.post(LLM_URL, json=payload, timeout=EXTRACTION_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"[MEMORY_EXTRACT] LLM returned {resp.status_code} for {chat_id}")
            return None

        data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()

        # Strip <think>...</think> tags if present (Dolphin sometimes emits these)
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

        # Try to extract JSON from the response (handle markdown code blocks)
        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            logger.warning(f"[MEMORY_EXTRACT] No JSON found in LLM response for {chat_id}: {raw[:100]}")
            return None

        extracted = json.loads(json_match.group())

        # Validate expected structure
        expected_keys = {"name", "age", "location", "relationship_status", "occupation",
                         "physical_description", "interests", "sexual_preferences",
                         "personal_facts", "emotional_state", "relationship_with_heather"}
        if not any(k in extracted for k in expected_keys):
            logger.warning(f"[MEMORY_EXTRACT] Extracted JSON missing expected keys for {chat_id}")
            return None

        logger.info(f"[MEMORY_EXTRACT] Extracted profile for {chat_id}: "
                     f"name={extracted.get('name')}, age={extracted.get('age')}, "
                     f"interests={len(extracted.get('interests', []))}, "
                     f"prefs={len(extracted.get('sexual_preferences', []))}, "
                     f"facts={len(extracted.get('personal_facts', []))}")
        return extracted

    except json.JSONDecodeError as e:
        logger.warning(f"[MEMORY_EXTRACT] JSON parse error for {chat_id}: {e}")
        return None
    except requests.exceptions.Timeout:
        logger.warning(f"[MEMORY_EXTRACT] Timeout ({EXTRACTION_TIMEOUT}s) for {chat_id}")
        return None
    except Exception as e:
        logger.error(f"[MEMORY_EXTRACT] Unexpected error for {chat_id}: {e}")
        return None


def _is_valid_extracted_name(name: str) -> bool:
    """Validate that an LLM-extracted name looks like a real human first name.

    Rejects:
    - Mixed-case gibberish (e.g. 'AUAdHd')
    - Single characters
    - Names with digits or special characters
    - Names longer than 20 chars (likely a phrase, not a name)
    """
    name = name.strip()
    if len(name) < 2 or len(name) > 20:
        return False
    # Must be only letters (and optionally a single space for two-part names)
    if not re.match(r'^[A-Za-z]+(\s[A-Za-z]+)?$', name):
        return False
    # Reject mixed-case gibberish: valid names are either "Mike", "mike", "MIKE",
    # "AJ", "DJ", "JR", or "De" — NOT "AUAdHd" or "tHiNkInG"
    # Each word must be: all-lower, all-upper (≤3 chars), or Title Case
    for word in name.split():
        if word.islower() or word.istitle():
            continue
        # Allow short all-caps (initials like AJ, DJ, JR, SK)
        if word.isupper() and len(word) <= 3:
            continue
        # Anything else is gibberish
        return False
    return True


def merge_extracted_profile(chat_id: int, extracted: dict):
    """Merge LLM-extracted profile data into existing user profile.

    - String fields: only update if new value is non-null and different
    - Age: only accept 18-99 range
    - List fields: append new items, deduplicate case-insensitively
    - Tracks extraction metadata (last_extraction_at, extraction_count)
    """
    profile = load_profile(chat_id)
    changes = []

    # String fields — update if new value is non-null and different
    string_fields = {
        "name": "name",
        "location": "location",
        "relationship_status": "relationship",
        "occupation": "occupation",
        "physical_description": "physical_description",
        "emotional_state": "emotional_state",
        "relationship_with_heather": "relationship_with_heather",
    }

    # Reject garbage names/locations — common words the LLM hallucinates
    _REJECTED_VALUES = {
        "go", "ready", "here", "come", "hard", "tight", "big", "hot", "yes", "no",
        "babe", "baby", "waiting", "you", "do", "in", "to", "good", "the", "a", "an",
        "it", "is", "on", "at", "my", "me", "i", "we", "so", "ok", "up", "out", "oh",
        "hi", "hey", "sure", "right", "just", "now", "well", "really", "want", "need",
        "like", "love", "fuck", "cum", "more", "sir", "daddy", "master", "null", "none",
        "unknown", "not specified", "n/a", "your tight", "public", "the shower",
        # Common verbs/adjectives the LLM hallucinates as names (found in 103 bad profiles)
        "all", "alone", "already", "also", "and", "assuming", "athletic", "aware",
        "back", "before", "blocking", "clean", "cock", "coming", "commando", "confused",
        "doing", "from", "gagged", "getting", "glad", "going", "gonna", "great",
        "grinding", "groan", "happy", "hoping", "horny", "how", "huge", "id",
        "imagining", "jerking", "live", "lol", "looking", "making", "new", "nice",
        "nowhere", "off", "on", "pretty", "releasing", "rock", "sharing", "sipping",
        "sit", "slutty", "still", "take", "talking", "thinking", "throbbing",
        "trying", "used", "very", "videos", "walking", "watch", "wherever", "while",
        "with", "your", "been", "being", "both", "but", "can", "could", "down",
        "each", "for", "had", "has", "have", "her", "his", "into", "its", "let",
        "may", "most", "much", "must", "not", "only", "other", "our", "over",
        "said", "she", "should", "some", "than", "that", "their", "them", "then",
        "there", "these", "they", "this", "was", "were", "what", "when", "which",
        "who", "will", "would", "about", "after", "again", "because", "before",
        "between", "could", "does", "during", "every", "first", "found", "from",
        "have", "into", "know", "last", "long", "look", "made", "many", "might",
        "never", "next", "open", "part", "pull", "push", "real", "same", "show",
        "tell", "turn", "under", "went", "work", "working", "feels", "feeling",
        "sitting", "standing", "waiting", "wearing", "wet", "wild", "young",
    }

    for ext_key, profile_key in string_fields.items():
        new_val = extracted.get(ext_key)
        if new_val and isinstance(new_val, str) and new_val.strip():
            new_val = new_val.strip()
            # Reject garbage values
            if new_val.lower() in _REJECTED_VALUES or len(new_val) < 2:
                continue
            # Name-specific validation: must look like a real name
            if profile_key == "name":
                if not _is_valid_extracted_name(new_val):
                    logger.debug(f"[MEMORY_MERGE] Rejected bad name: {new_val!r} for {chat_id}")
                    continue
                # Title-case valid names (e.g. "jeff" -> "Jeff")
                new_val = new_val.strip().title()
            old_val = profile.get(profile_key)
            if new_val != old_val:
                profile[profile_key] = new_val
                changes.append(f"{profile_key}: {old_val!r} -> {new_val!r}")

    # Age — only accept 18-99 range
    new_age = extracted.get("age")
    if new_age is not None:
        try:
            age_int = int(new_age)
            if 18 <= age_int <= 99:
                old_age = profile.get("age")
                new_age_str = str(age_int)
                if new_age_str != old_age:
                    profile["age"] = new_age_str
                    changes.append(f"age: {old_age!r} -> {new_age_str!r}")
        except (ValueError, TypeError):
            pass

    # List fields — append new items, deduplicate case-insensitively
    list_fields = {
        "interests": "interests",
        "sexual_preferences": "sexual_preferences",
        "personal_facts": "personal_facts",
    }

    for ext_key, profile_key in list_fields.items():
        new_items = extracted.get(ext_key, [])
        if not isinstance(new_items, list):
            continue

        if profile_key not in profile:
            profile[profile_key] = []

        existing = profile[profile_key]
        existing_lower = {item.lower() for item in existing if isinstance(item, str)}

        added = []
        for item in new_items:
            if isinstance(item, str) and item.strip():
                item = item.strip()
                if item.lower() not in existing_lower:
                    existing.append(item)
                    existing_lower.add(item.lower())
                    added.append(item)

        if added:
            changes.append(f"{profile_key}: +{len(added)} ({', '.join(added[:3])})")

    # Track extraction metadata
    profile["last_extraction_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    profile["extraction_count"] = profile.get("extraction_count", 0) + 1

    if changes:
        save_profile(chat_id, force=True)
        logger.info(f"[MEMORY_MERGE] {chat_id}: {len(changes)} changes — {'; '.join(changes[:5])}")
    else:
        logger.debug(f"[MEMORY_MERGE] {chat_id}: No new data from extraction #{profile['extraction_count']}")


def build_memory_tease(chat_id: int) -> Optional[str]:
    """Build a personalized upsell tease showing what Heather 'remembers' about the user.

    Returns a tease string if we have enough data, or None if profile is too thin.
    Needs at least 2 pieces of info to craft a compelling tease.
    """
    profile = load_profile(chat_id)

    pieces = []

    name = profile.get("name")
    if name:
        pieces.append(("name", name))

    location = profile.get("location")
    if location:
        pieces.append(("location", location))

    # Sexual preferences (from LLM extraction)
    sex_prefs = profile.get("sexual_preferences", [])
    if sex_prefs:
        pieces.append(("pref", random.choice(sex_prefs)))

    # Interests (from LLM extraction)
    interests = profile.get("interests", [])
    if interests:
        pieces.append(("interest", random.choice(interests)))

    # Personal facts
    personal_facts = profile.get("personal_facts", [])
    if personal_facts:
        pieces.append(("fact", random.choice(personal_facts)))

    # Top kink as fallback
    top_kinks = get_top_kinks(chat_id, 1)
    if top_kinks:
        pieces.append(("kink", top_kinks[0][0]))

    if len(pieces) < 2:
        return None

    # Build tease from available pieces
    templates = []

    # Name + preference combo
    name_piece = next((v for t, v in pieces if t == "name"), None)
    pref_piece = next((v for t, v in pieces if t == "pref"), None)
    kink_piece = next((v for t, v in pieces if t == "kink"), None)
    loc_piece = next((v for t, v in pieces if t == "location"), None)
    interest_piece = next((v for t, v in pieces if t == "interest"), None)
    fact_piece = next((v for t, v in pieces if t == "fact"), None)

    if name_piece and pref_piece:
        templates.append(
            f"mmm I know your name's {name_piece}, and I definitely know you're into {pref_piece} "
            f"\U0001f60f upgrade and I won't hold back... https://t.me/HeatherCoffeebot?start=tip"
        )
    if name_piece and kink_piece:
        templates.append(
            f"hey {name_piece}... I remember what gets you going \U0001f608 "
            f"unlock the full me and I'll put that {kink_piece} obsession to GOOD use \U0001f525 "
            f"https://t.me/HeatherCoffeebot?start=tip"
        )
    if name_piece and loc_piece:
        templates.append(
            f"I remember you {name_piece}... from {loc_piece} right? \U0001f60f "
            f"imagine what I'd remember about you with full access... "
            f"https://t.me/HeatherCoffeebot?start=tip"
        )
    if pref_piece and fact_piece:
        templates.append(
            f"oh I remember you baby \U0001f608 I know you're into {pref_piece} and {fact_piece}... "
            f"the FULL uncensored me remembers everything \U0001f525 "
            f"https://t.me/HeatherCoffeebot?start=tip"
        )
    if name_piece and interest_piece:
        templates.append(
            f"I haven't forgotten about you {name_piece} \U0001f48b "
            f"the {interest_piece} lover who wants to see more of me... "
            f"unlock everything: https://t.me/HeatherCoffeebot?start=tip"
        )

    if not templates:
        # Generic fallback with whatever we have
        detail_strs = [v for _, v in pieces[:2]]
        templates.append(
            f"I remember things about you baby... like {' and '.join(detail_strs)} \U0001f60f "
            f"upgrade and the real Heather comes out \U0001f525 "
            f"https://t.me/HeatherCoffeebot?start=tip"
        )

    return random.choice(templates)


# ── Prompt Builder ───────────────────────────────────────────────────

def build_profile_prompt(chat_id: int, access_tier: str = "FREE") -> str:
    """Build a system prompt injection summarizing this user's profile.
    Returns empty string for FREE tier (except name injection if known).
    Returns empty string if profile is too thin to be useful."""
    profile = load_profile(chat_id)

    # FREE users — minimal personalization: inject name only if we know it
    if access_tier == "FREE":
        name = profile.get("name")
        if name:
            return f"\n\n[The user's name is {name}. Use it naturally.]"
        return ""

    # Don't inject until we have meaningful data
    if profile["total_msgs"] < 5:
        return ""

    parts = []

    # Name and basics
    basics = []
    if profile["name"]:
        basics.append(f"His name is {profile['name']}")
    if profile["age"]:
        basics.append(f"age {profile['age']}")
    if profile["location"]:
        basics.append(f"from {profile['location']}")
    if profile["relationship"]:
        basics.append(profile["relationship"])
    # LLM-extracted occupation
    if profile.get("occupation"):
        basics.append(f"works as {profile['occupation']}")
    if basics:
        parts.append(", ".join(basics) + ".")

    # Cock details
    cock = profile.get("cock", {})
    cock_parts = []
    if cock.get("size"):
        cock_parts.append(cock["size"])
    if cock.get("description"):
        cock_parts.append(cock["description"])
    if cock_parts:
        parts.append(f"His cock: {', '.join(cock_parts)}.")

    # Top kinks
    top = get_top_kinks(chat_id, 5)
    if top:
        kink_strs = [f"{k} ({v})" for k, v in top]
        parts.append(f"Biggest turn-ons: {', '.join(kink_strs)}.")

    # LLM-extracted interests
    interests = profile.get("interests", [])
    if interests:
        parts.append(f"Interests: {', '.join(interests[-5:])}.")

    # LLM-extracted sexual preferences
    sex_prefs = profile.get("sexual_preferences", [])
    if sex_prefs:
        parts.append(f"Sexual preferences: {', '.join(sex_prefs[-5:])}.")

    # Personal notes (regex-extracted)
    if profile["personal_notes"]:
        notes = profile["personal_notes"][-5:]
        parts.append(f"Personal: {'; '.join(notes)}.")

    # What Heather has shared (for consistency)
    if profile["heather_shared"]:
        shared = profile["heather_shared"][-5:]
        parts.append(f"He already knows about: {', '.join(shared)}.")

    # Recent session memories (last 3) — filter out useless generic summaries
    session_mems = profile.get("session_memories", [])
    if session_mems:
        recent = session_mems[-3:]
        mem_strs = []
        for mem in recent:
            if isinstance(mem, dict):
                summary = mem.get('summary', '')
                # Filter out generic summaries that add no value
                if summary and "identity is not specified" not in summary.lower():
                    mem_strs.append(f"{mem.get('date', '?')}: {summary}")
        if mem_strs:
            parts.append("Recent sessions:\n" + "\n".join(f"  - {m}" for m in mem_strs))

    # Memorable moments/quotes (last 3)
    memorables = profile.get("memorable", [])
    if memorables:
        recent_mems = memorables[-3:]
        mem_strs = []
        for mem in recent_mems:
            if isinstance(mem, dict):
                mem_strs.append(f"\"{mem.get('text', '')}\" ({mem.get('date', '?')})")
            elif isinstance(mem, str):
                mem_strs.append(f"\"{mem}\"")
        if mem_strs:
            parts.append(f"Things he's said: {'; '.join(mem_strs)}.")

    # VIP-only deep profile fields (personal_facts, emotional_state, relationship_with_heather)
    if access_tier == "VIP":
        personal_facts = profile.get("personal_facts", [])
        if personal_facts:
            parts.append(f"Known facts: {'; '.join(personal_facts[-5:])}.")

        emotional_state = profile.get("emotional_state")
        if emotional_state:
            parts.append(f"Current vibe: {emotional_state}.")

        rel_with_heather = profile.get("relationship_with_heather")
        if rel_with_heather:
            parts.append(f"His view of you: {rel_with_heather}.")

    # Session stats
    sessions = profile.get("sessions", 0)
    total = profile.get("total_msgs", 0)
    if sessions > 1:
        # Calculate days since first seen
        first = profile.get("first_seen")
        last = profile.get("last_seen")
        if first and last and first != last:
            parts.append(f"Returning chatter: {sessions} sessions, {total} total msgs (since {first}).")
        else:
            parts.append(f"Returning chatter: {sessions} sessions, {total} total msgs.")
    elif total >= 10:
        parts.append(f"Active session: {total} msgs so far.")

    if not parts:
        return ""

    summary = " ".join(parts)
    name = profile.get("name")
    name_instruction = f"ALWAYS call him {name} (use his name at least once). " if name else ""
    # Deep history recall — generate a natural memory hook from past sessions
    history_hook = _build_history_recall(profile)
    if history_hook:
        parts.append(history_hook)

    summary = " ".join(parts)

    prompt = (
        f"\n\n[USER PROFILE: {summary} "
        f"Use this to personalize — {name_instruction}lean into his kinks, "
        f"remember details he shared. Don't repeat things he already knows about you. "
        f"Make him feel known and special. "
        f"If this is a returning user, reference something specific from past sessions naturally — "
        f"like a friend who remembers.]"
    )

    # Add callback prompt if eligible
    if _should_inject_callback(chat_id):
        prompt += _build_callback_prompt(chat_id)
        logger.info(f"[MEMORY] Injected callback prompt for {chat_id}")

    return prompt
