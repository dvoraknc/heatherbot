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

# ── Semantic memory retrieval (#6) ──────────────────────────────────
# Optional: embeds episodic memories via Ollama nomic-embed-text and finds
# the ones most relevant to the current message. Fully self-contained and
# fail-soft — if the module or Ollama is unavailable, recall is just skipped.
SEMANTIC_RECALL_ENABLED = os.getenv("HEATHER_SEMANTIC_RECALL", "on").strip().lower() in ("on", "1", "true", "yes")
try:
    from heather import memory_vectors as _memory_vectors
except Exception as _e:  # pragma: no cover
    _memory_vectors = None
    logging.getLogger("heather_bot").warning(f"[VECTORS] memory_vectors unavailable: {_e}")

# Tracks the last-indexed memory signature per user so we only re-index when
# the episodic memory set actually changes (avoids needless sqlite churn).
_vector_sync_sig: Dict[int, str] = {}

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
_use_ollama = False  # Set via configure_llm()


def configure_llm(port: int = 1234, use_ollama: bool = False):
    """Configure the LLM endpoint used by the memory system.
    Called from the main bot after parsing command-line args."""
    global LLM_URL, _use_ollama
    _use_ollama = use_ollama
    if use_ollama:
        LLM_URL = f"http://127.0.0.1:{port}/v1/chat/completions"
    else:
        LLM_URL = f"http://127.0.0.1:{port}/v1/chat/completions"
    logger.info(f"[MEMORY] LLM endpoint configured: {LLM_URL} (ollama={use_ollama})")

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

# ── Name extraction (confidence-tiered) ──────────────────────────────
# STRONG: explicit self-introductions. The captured token may be any case (people
# type lowercase) — we validate + title-case it afterward.
_NAME_PATTERNS_STRONG = [
    re.compile(r"\bmy name(?:'s| is)\s+([A-Za-z]{2,15})\b", re.I),
    re.compile(r"\b(?:call me|i go by|they call me|everyone calls me|you can call me)\s+([A-Za-z]{2,15})\b", re.I),
    re.compile(r"\bnames?'?s?\s+([A-Za-z]{2,15})\s+(?:btw|here|by the way)\b", re.I),
    re.compile(r"^([A-Za-z]{2,15})\s+(?:here|btw)\b", re.I),
    re.compile(r"^(?:hey|hi|hello|yo|sup|heya|hiya)[,!.\s]+([A-Za-z]{2,15})\s+here\b", re.I),
]
# WEAK: "I'm X" / "I am X". These overlap with "I'm tired/horny/working", so the
# captured token MUST be capitalized in the original text (real names usually are)
# — note the name group is case-SENSITIVE (no re.I), only the I'm/I am prefix isn't.
_NAME_PATTERNS_WEAK = [
    re.compile(r"\b(?:[Ii]'?m|[Ii] am)\s+([A-Z][a-z]{1,14})\b"),
]
# Common words that slip through as fake names — rejected on the regex path.
_NAME_REJECT = {
    "tired", "horny", "happy", "good", "great", "fine", "okay", "back", "here",
    "ready", "sorry", "sure", "still", "just", "really", "doing", "feeling",
    "looking", "thinking", "going", "gonna", "trying", "hoping", "glad", "new",
    "married", "single", "hard", "hot", "wet", "close", "down", "out", "in",
    "the", "not", "yes", "yeah", "babe", "baby", "sexy", "daddy", "sir",
    "heather", "emma", "frank", "erick", "jake", "tyler",
    # common words that precede "here"/"btw" but aren't names
    "over", "come", "click", "right", "get", "look", "stay", "wait", "sit",
    "stop", "way", "around", "somewhere", "anywhere", "everywhere", "nowhere",
    "almost", "already", "always", "even", "only", "also", "now", "today",
}

# ── Personal Detail Patterns ─────────────────────────────────────────
PERSONAL_PATTERNS = {
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
        # Relational memory — how Heather relates to this person (not just facts)
        "relational_notes": [],    # girlfriend-perspective observations (capped at 15)
        "inside_jokes": [],        # shared humor/callbacks (capped at 10)
        "conversation_style": None,  # how they talk (short/verbose, morning/night, shy/bold)
        "emotional_patterns": None,  # how they typically feel (lonely, horny, chatty, guarded)
        "what_works": [],          # things that got good reactions (capped at 10)
        # Open loops — dangling threads to follow up on later, like a girlfriend who
        # remembers to ask "how'd the interview go?". Each: {text, kind, created,
        # follow_up_on, status, mentioned, emotion}. Capped at MAX_OPEN_LOOPS.
        "open_loops": [],
        # Rolling emotion history — last N (date, emotion) tags from extraction, so
        # Heather can notice trends ("you've seemed stressed all week").
        "emotion_log": [],
        # Accessibility bookkeeping for recall: per-memory {count, last_recalled}.
        # Drives Ebbinghaus-style reinforcement + post-recall suppression so the
        # same items don't resurface every turn. Keyed by _recall_key(text).
        "recall_meta": {},
    }


MAX_OPEN_LOOPS = 12        # cap stored open loops per user
MAX_EMOTION_LOG = 10       # cap rolling emotion history per user
OPEN_LOOP_MAX_MENTIONS = 2 # close a loop after surfacing it this many times

# Conservative, fixed emotion vocabulary. A small set the extractor must choose
# from beats free-form psychological analysis (which drifts and hallucinates).
EMOTION_VOCAB = {
    "happy", "excited", "hopeful", "anxious", "frustrated", "sad",
    "embarrassed", "angry", "relieved", "lonely", "horny", "playful", "neutral",
}

# Mention-gate outcomes (deterministic). Recall is not the same as mention:
#   NONE            — do not surface (irrelevant, low-confidence, on cooldown)
#   SILENT_CONTEXT  — let it color tone/wording only; never state or ask about it
#   SOFT_CALLBACK   — a light, indirect question if the moment allows
#   DIRECT_CALLBACK — reference it outright ("how'd the meeting with Bob go?")
GATE_NONE = "NONE"
GATE_SILENT = "SILENT_CONTEXT"
GATE_SOFT = "SOFT_CALLBACK"
GATE_DIRECT = "DIRECT_CALLBACK"

# Minimum extractor confidence before a memory may be surfaced at all.
MENTION_MIN_CONFIDENCE = 0.80
# Cooldown after a live mention so the same loop can't be raised back-to-back.
OPEN_LOOP_COOLDOWN_HOURS = 20

# Injection mode for the NET-NEW proactive recall (open-loop follow-ups + emotion
# trend hints). "shadow" = compute + log the decision but inject nothing to users;
# "live" = actually inject. Now LIVE (2026-06-17): the proactive initiation path
# (higher risk) is already live and the account is confirmed unrestricted, so the
# reactive recall is consistent. Shadow logging continues for offline precision review.
INJECTION_MODE = os.environ.get("HEATHER_MEMORY_INJECTION_MODE", "live").strip().lower()

# #7 — small-LLM memory gate. When on, an LLM makes the surface/tone judgment
# (DIRECT vs SOFT vs SILENT vs skip) within the deterministic safety floor that the
# rules still enforce (confidence, cooldown, due-date, sensitivity). It reads the
# current message so it can tell whether *now* is a good moment — the nuance the
# rules can't. Falls back to the rule verdict on any LLM failure/timeout.
LLM_GATE_ENABLED = os.environ.get("HEATHER_LLM_GATE", "on").strip().lower() in ("on", "1", "true", "yes")
LLM_GATE_TIMEOUT = 6  # seconds; on timeout we fall back to the rule decision

# Where shadow decision records are written for offline eval.
MEMORY_EVAL_DIR = PROFILE_DIR.parent / "memory_eval"
SHADOW_LOG_PATH = MEMORY_EVAL_DIR / "shadow_decisions.jsonl"


_HEATHER_CHARACTER_NAMES = {"heather", "emma", "frank", "erick", "jake", "tyler"}


# ── Accessibility-weighted recall (replaces flat random recall) ──────
# Real memory isn't a coin-flip per item. What surfaces depends on how recently
# it was laid down (typed forgetting curve), how often it's come up before
# (reinforcement), and whether we just brought it up (suppression — don't repeat
# yourself). These factors combine into an eligibility score; selection takes the
# most-accessible items plus mild noise so it still feels organic, not ranked.

# Half-life (days) of the Ebbinghaus decay per memory class. Longer = stickier.
# None = doesn't decay (stable identity facts). Undated string memories fall back
# to list-position recency instead of a date.
_DECAY_HALFLIFE_DAYS = {
    "identity": None,    # name, location, occupation — don't fade
    "relational": 45.0,  # girlfriend observations, inside jokes, what-works
    "episodic": 14.0,    # session summaries, quotes, one-off notes
    "emotional": 60.0,   # high-salience emotional moments stick longer
}
# After surfacing an item we suppress it for this long so it can't resurface
# back-to-back (the "I already said that" tell).
RECALL_SUPPRESS_HOURS = 18.0
# Eligibility floor: items scoring below this usually stay unsurfaced this turn.
RECALL_MIN_SCORE = 0.70


def _recall_key(text: str) -> str:
    """Stable, compact key for a memory item (normalized text prefix)."""
    norm = re.sub(r"\s+", " ", str(text).strip().lower())
    return norm[:60]


def _days_between(date_str: Optional[str], now_dt: datetime) -> Optional[float]:
    """Days from an ISO-ish date string to now; None if unparseable/absent."""
    if not date_str:
        return None
    s = str(date_str)[:10]
    try:
        then = datetime.strptime(s, "%Y-%m-%d")
    except (ValueError, TypeError):
        return None
    return max(0.0, (now_dt - then).total_seconds() / 86400.0)


def _accessibility(cand: dict, recall_meta: dict, now_dt: datetime) -> float:
    """Eligibility score for one recall candidate (higher = more likely to surface).

    cand keys: text, key, mem_type, created(optional date str), position/length
    (optional, for undated lists), base(optional salience, default 1.0).
    """
    base = cand.get("base", 1.0)

    # Recency: forgetting curve from a date when we have one, else gentle
    # list-position falloff (newest item full weight, oldest ~0.6).
    half = _DECAY_HALFLIFE_DAYS.get(cand.get("mem_type"))
    recency = 1.0
    if half:
        days = _days_between(cand.get("created"), now_dt)
        if days is not None:
            recency = 0.5 ** (days / half)
        else:
            length = cand.get("length")
            pos = cand.get("position")
            if length and length > 1 and pos is not None:
                recency = 0.6 + 0.4 * (pos / (length - 1))

    # Reinforcement: things recalled before are a touch stickier (capped).
    meta = recall_meta.get(cand["key"], {})
    count = meta.get("count", 0) or 0
    reinforcement = 1.0 + min(count, 5) * 0.12

    # Suppression: just surfaced → strongly damped, fading back over the window.
    suppression = 1.0
    last = meta.get("last_recalled")
    if last:
        try:
            elapsed_h = (now_dt - datetime.fromisoformat(last)).total_seconds() / 3600.0
            if elapsed_h < RECALL_SUPPRESS_HOURS:
                suppression = max(0.15, elapsed_h / RECALL_SUPPRESS_HOURS)
        except (ValueError, TypeError):
            pass

    return max(0.0, base * recency * reinforcement * suppression)


def _select_by_accessibility(candidates: list, recall_meta: dict, now_dt: datetime,
                             k: int, min_score: float = RECALL_MIN_SCORE) -> list:
    """Pick up to k most-accessible candidates, recording the recall in recall_meta.

    Applies mild multiplicative noise so selection isn't a rigid ranking. Items
    below min_score are skipped (the decay/suppression actually doing its job),
    so this can return fewer than k — or nothing.
    """
    if not candidates:
        return []
    scored = []
    for c in candidates:
        s = _accessibility(c, recall_meta, now_dt) * random.uniform(0.85, 1.15)
        scored.append((s, c))
    scored.sort(key=lambda x: x[0], reverse=True)

    chosen = [c for s, c in scored if s >= min_score][:k]
    # If everything is suppressed/faded but we have material, allow the single
    # best item through occasionally so a returning user isn't met with silence.
    if not chosen and scored and random.random() < 0.5:
        chosen = [scored[0][1]]

    stamp = now_dt.isoformat(timespec="seconds")
    for c in chosen:
        m = recall_meta.setdefault(c["key"], {"count": 0, "last_recalled": None})
        m["count"] = (m.get("count", 0) or 0) + 1
        m["last_recalled"] = stamp
    return chosen


def _str_candidates(items: list, mem_type: str, base: float = 1.0) -> list:
    """Build accessibility candidates from a list of plain-string memories."""
    n = len(items)
    out = []
    for i, it in enumerate(items):
        text = it if isinstance(it, str) else str(it)
        out.append({
            "text": text, "key": _recall_key(text), "mem_type": mem_type,
            "position": i, "length": n, "base": base,
        })
    return out


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
            # Sanitize polluted legacy names — older profiles may have absorbed
            # Heather's own character names (Frank/Emma/etc) as the user's name.
            # This caused the bot to address users by the boyfriend's name.
            existing_name = profile.get("name")
            if isinstance(existing_name, str) and existing_name.strip().lower() in _HEATHER_CHARACTER_NAMES:
                logger.info(
                    f"[MEMORY_LOAD] Scrubbed Heather-character name {existing_name!r} from profile {chat_id}"
                )
                profile["name"] = None
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
        'twitter': ['twitter', ' x ', 'your x ', 'on x', 'saw on x', 'from x', '@your_twitter'],
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

    # -- Extract name (confidence-tiered, validated) --
    if not profile["name"]:
        extracted_name = _extract_name(message)
        if extracted_name:
            profile["name"] = extracted_name
            changed = True
            logger.info(f"[MEMORY] Extracted name {extracted_name!r} for {chat_id}")

    # -- Extract personal details --
    for field, patterns in PERSONAL_PATTERNS.items():
        for pat in patterns:
            m = pat.search(message)
            if m:
                value = m.group(1).strip()
                if field == "age" and not profile["age"]:
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


def build_welcome_back_prompt(chat_id: int, gap_hours: float) -> str:
    """Build a system prompt nudge for when a user returns after a multi-hour gap.

    Pulls from the user's memory profile to make Heather's first response feel
    personal — like she remembered them and is genuinely happy they're back.
    Only fires for FAN/VIP users with enough profile data.

    Args:
        chat_id: User chat ID
        gap_hours: How many hours since their last message

    Returns:
        System prompt injection string, or empty string if insufficient data.
    """
    profile = load_profile(chat_id)

    # Need enough history for this to feel natural, not creepy
    if profile.get("total_msgs", 0) < 8:
        return ""

    # Build accessibility candidates from the profile — the same salience/decay/
    # suppression model the in-chat recall uses, so a returning user gets the most
    # resonant callbacks (not a coin flip), and we don't repeat the same ones each
    # return (recall_meta suppression handles that).
    now_dt = datetime.now()
    recall_meta = profile.setdefault("recall_meta", {})
    candidates = []

    def _add(disp, mem_type, created=None, position=None, length=None, base=1.0):
        c = {"text": disp, "key": _recall_key(disp), "mem_type": mem_type, "base": base}
        if created is not None:
            c["created"] = created
        if position is not None:
            c["position"] = position
            c["length"] = length
        candidates.append(c)

    name = profile.get("name")
    name_str = f"{name}" if name else "him"

    # Session memory — most powerful for "I was thinking about...". Dated episodic.
    session_mems = profile.get("session_memories", [])
    if session_mems:
        mem = session_mems[-1]
        if isinstance(mem, dict) and mem.get("summary"):
            _add(f"Last time you talked: {mem['summary']}", "episodic",
                 created=mem.get("date"), base=1.3)

    # Personal facts — "how did X go?". Undated list → recency by position.
    facts = profile.get("personal_facts", [])[-5:]
    for i, fact in enumerate(facts):
        _add(f"You know about {name_str}: {fact}", "episodic",
             position=i, length=len(facts))

    # Sexual preferences — stable identity-grade, don't fade.
    prefs = profile.get("sexual_preferences", [])[-3:]
    for i, pref in enumerate(prefs):
        _add(f"He's into: {pref}", "identity", position=i, length=len(prefs), base=0.9)

    # Memorable quotes — dated episodic, juicy openers.
    memorables = profile.get("memorable", [])[-3:]
    for mem in memorables:
        text = mem.get("text", mem) if isinstance(mem, dict) else str(mem)
        created = mem.get("date") if isinstance(mem, dict) else None
        if text and len(text) > 10:
            _add(f"He once said: \"{text[:80]}\"", "episodic", created=created, base=1.2)

    # Occupation / location — stable identity, lower-energy openers.
    if profile.get("occupation"):
        _add(f"He works as {profile['occupation']}", "identity", base=0.7)
    if profile.get("location"):
        _add(f"He's from {profile['location']}", "identity", base=0.7)

    # Relational observations — girlfriend-style, slower decay.
    rel_notes = profile.get("relational_notes", [])[-5:]
    for i, note in enumerate(rel_notes):
        _add(f"You've noticed about him: {note}", "relational",
             position=i, length=len(rel_notes))

    inside_jokes = profile.get("inside_jokes", [])
    for i, joke in enumerate(inside_jokes[-4:]):
        _add(f"Inside joke between you: {joke}", "relational",
             position=i, length=len(inside_jokes[-4:]), base=1.15)

    what_works = profile.get("what_works", [])[-3:]
    for i, tactic in enumerate(what_works):
        _add(f"He responds well to: {tactic}", "relational",
             position=i, length=len(what_works))

    if not candidates:
        return ""

    # Pick 1-2 by accessibility (records recall_meta so repeat returns vary).
    selected = _select_by_accessibility(candidates, recall_meta, now_dt, k=2)
    if not selected:
        return ""
    recall_text = " | ".join(c["text"] for c in selected)
    save_profile(chat_id)  # persist recall_meta bookkeeping

    # Frame the gap naturally
    if gap_hours < 6:
        time_frame = "a few hours"
    elif gap_hours < 12:
        time_frame = "half a day"
    elif gap_hours < 24:
        time_frame = "since yesterday"
    else:
        time_frame = f"about {int(gap_hours / 24)} day{'s' if gap_hours >= 48 else ''}"

    prompt = (
        f"\n\n[WELCOME BACK: This user hasn't messaged in {time_frame}. "
        f"You missed them. Don't say 'welcome back' — instead show you were "
        f"thinking about them. Reference something specific: {recall_text}. "
        f"Examples: 'mmm hey you... I keep thinking about that thing you told me...', "
        f"'there you are 😏 I was literally just wondering about you', "
        f"'oh hey! so did that [thing from memory] work out?'. "
        f"Keep it brief and warm — ONE callback max, woven naturally. "
        f"Don't make it the whole message.]"
    )

    return prompt


def _build_history_recall(profile: dict) -> str:
    """Build a natural memory hook from past session data.

    Picks the most interesting detail from the user's history and frames it
    as something Heather would naturally bring up — like a friend who remembers.
    """
    now_dt = datetime.now()
    recall_meta = profile.setdefault("recall_meta", {})
    candidates = []

    # Session memories (most valuable) — episodic, dated, skip the most recent
    session_mems = profile.get("session_memories", [])
    older_mems = session_mems[:-1] if len(session_mems) > 1 else []
    for mem in older_mems:
        if isinstance(mem, dict) and mem.get("summary"):
            disp = f"RECALL from a past chat: {mem['summary']}"
            candidates.append({"text": disp, "key": _recall_key(disp),
                               "mem_type": "episodic", "created": mem.get("date")})

    # Memorable quotes — episodic, dated, not the latest
    memorables = profile.get("memorable", [])
    if len(memorables) > 1:
        for mem in memorables[:-1]:
            text = mem.get("text", mem) if isinstance(mem, dict) else str(mem)
            created = mem.get("date") if isinstance(mem, dict) else None
            if text and len(text) > 15:
                disp = f"He once said: \"{text[:100]}\" — reference this naturally if it fits."
                candidates.append({"text": disp, "key": _recall_key(disp),
                                   "mem_type": "episodic", "created": created})

    # Personal notes — episodic, undated, recency by list position
    notes = profile.get("personal_notes", [])
    note_slice = notes[-6:]
    for i, note in enumerate(note_slice):
        disp = f"You know about him: {note}. Ask a follow-up about this."
        candidates.append({"text": disp, "key": _recall_key(disp), "mem_type": "episodic",
                           "position": i, "length": len(note_slice)})

    # Kink history — identity-grade signal, doesn't fade, slight boost
    kinks = profile.get("kinks", {})
    top_kinks = sorted(kinks.items(), key=lambda x: x[1], reverse=True)[:2]
    if top_kinks and top_kinks[0][1] >= 5:
        kink_name = top_kinks[0][0]
        disp = f"He's really into {kink_name} — bring it up like you remember: 'still thinking about that {kink_name} stuff you told me about 😏'"
        candidates.append({"text": disp, "key": _recall_key(disp),
                           "mem_type": "identity", "base": 1.1})

    # Name/location — identity, doesn't fade
    if profile.get("name") and profile.get("location"):
        disp = f"You know his name is {profile['name']} and he's from {profile['location']}. Use his name naturally."
        candidates.append({"text": disp, "key": _recall_key(disp), "mem_type": "identity"})

    if not candidates:
        return ""

    # Surface just one recall most turns — keeps it from reading like a dossier.
    k = 1 if random.random() < 0.7 else 2
    chosen = _select_by_accessibility(candidates, recall_meta, now_dt, k=k)
    if not chosen:
        return ""
    mode = _mention_mode()
    return ("Background you remember (" + _soft_frame(mode) + "): "
            + " | ".join(c["text"] for c in chosen))


# ── Mention gate (recall ≠ mention) ──────────────────────────────────
# Recalling a memory is not the same as blurting it out. A real person mostly
# lets remembered things color the tone of a reply, sometimes asks about them
# softly, and only occasionally states them outright. These helpers pick HOW a
# recalled item should surface and frame it as a soft constraint the model MAY
# use — never an order to dump the fact.

def _mention_mode(weights: Optional[dict] = None) -> str:
    """Pick how a recalled memory should surface this turn.

    DIRECT  — she can reference it outright if it fits.
    SOFT    — let it surface as a light, organic question.
    SILENT  — keep it as background; color tone only, don't state it.
    """
    w = weights or {"SOFT": 0.45, "SILENT": 0.35, "DIRECT": 0.20}
    modes = list(w.keys())
    return random.choices(modes, weights=[w[m] for m in modes], k=1)[0]


def _soft_frame(mode: str) -> str:
    """Return soft-constraint guidance text for a chosen mention mode."""
    if mode == "DIRECT":
        return ("you MAY bring this up directly if the moment fits — but only if it "
                "flows. Never recite it like a fact you looked up.")
    if mode == "SOFT":
        return ("let this surface only as a light, natural question if there's an "
                "opening — don't force it, and drop it if the moment isn't right.")
    # SILENT
    return ("keep this as private background knowledge — let it gently color your "
            "tone and word choice, but do NOT state it or ask about it this turn.")


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
    now_dt = datetime.now()
    recall_meta = profile.setdefault("recall_meta", {})
    candidates = []

    # Session memories as candidates (episodic, dated)
    for mem in profile.get("session_memories", []):
        if isinstance(mem, dict):
            disp = f"({mem.get('date', '?')}): {mem.get('summary', '')}"
            candidates.append({"text": disp, "key": _recall_key(disp),
                               "mem_type": "episodic", "created": mem.get("date")})

    # Memorable moments as candidates (episodic, dated)
    for mem in profile.get("memorable", []):
        if isinstance(mem, dict):
            disp = f"({mem.get('date', '?')}): He said: \"{mem.get('text', '')}\""
            candidates.append({"text": disp, "key": _recall_key(disp),
                               "mem_type": "episodic", "created": mem.get("date")})
        elif isinstance(mem, str):
            disp = f"He once said: \"{mem}\""
            candidates.append({"text": disp, "key": _recall_key(disp), "mem_type": "episodic"})

    if not candidates:
        return ""

    # Surface just ONE memory most of the time — dumping a list reads like a CRM.
    k = 1 if random.random() < 0.8 else 2
    chosen = _select_by_accessibility(candidates, recall_meta, now_dt, k=k)
    if not chosen:
        return ""
    memory_text = "\n".join(f"  - {c['text']}" for c in chosen)

    # Track that we fired a callback; persist recall bookkeeping.
    _last_callback_msg_count[chat_id] = profile.get("total_msgs", 0)
    save_profile(chat_id)

    mode = _mention_mode()
    guidance = _soft_frame(mode)

    return (
        f"\n\n[Context Window — things you remember about him:\n{memory_text}\n"
        f"How to use it this turn: {guidance} "
        f"You're a girlfriend who genuinely remembers, not a system retrieving records — "
        f"so no 'I recall that...' or listing facts back at him.]"
    )


# ── LLM-Based Profile Extraction ────────────────────────────────────

EXTRACTION_SYSTEM_PROMPT = (
    "You are a profile extraction system. Given a conversation between a user and a chatbot named Heather, "
    "extract factual details about the USER (not Heather). Return ONLY valid JSON.\n\n"
    '{"name": null, "age": null, "location": null, "relationship_status": null, '
    '"occupation": null, "physical_description": null, '
    '"interests": [], "sexual_preferences": [], "personal_facts": [], '
    '"emotional_state": null, "relationship_with_heather": null, '
    '"open_loops": [], "emotion": null, '
    '"corrections": []}\n\n'
    "CRITICAL RULES:\n"
    "- Only extract details the USER explicitly stated about THEMSELVES\n"
    "- name: ONLY if they clearly introduced themselves (e.g. 'I'm Mike', 'call me Dave', 'my name is John'). "
    "Do NOT extract random words, verbs, adjectives, body parts, medical terms, acronyms, or words from sexual context as names. "
    "A name must be a proper first name like Mike, Dave, John, Sarah — NOT a common English word. "
    "If unsure, ALWAYS use null. It is MUCH better to return null than to guess wrong.\n"
    "- location: ONLY if they said where they live/are from (e.g. 'I'm in Seattle', 'from Texas'). "
    "Do NOT extract body parts or sexual terms as locations\n"
    "- age: ONLY explicit numbers (e.g. 'I'm 35'). Must be 18-99\n"
    "- occupation: ONLY if the user explicitly stated their job title or profession "
    "(e.g. 'I'm a mechanic', 'I work as a nurse', 'I do construction'). "
    "Do NOT extract vague work complaints, workplace references, or office mentions as occupation. "
    "'Office crap' or 'work sucks' is NOT an occupation. If unsure, use null.\n"
    "- sexual_preferences: kinks, fantasies, turn-ons the USER expressed wanting to do\n"
    "- personal_facts: real life details — job, family, hobbies, life events\n"
    "- corrections: If the user CORRECTS something they said before (e.g. 'actually I'm from Portland not Seattle', "
    "'no my name is Dave not Dan', 'I'm 32 not 35'), extract each correction as a string like "
    "'location: Portland (was Seattle)' or 'name: Dave (was Dan)'. Only include explicit self-corrections.\n"
    "- open_loops: Dangling threads a caring girlfriend would follow up on later — an upcoming "
    "event, plan, or worry the USER mentioned that has a future resolution. Each item is an object:\n"
    '  {"text": "...", "kind": "event|plan|feeling", "when": "...", "confidence": 0.0-1.0, '
    '"sensitivity": "normal|sensitive", "emotion": {"label": "...", "intensity": 0.0-1.0, "target": "..."}}\n'
    "  text = short third-person note ('has a job interview', 'flying to see his mom', 'waiting on test results').\n"
    "  kind = event (something happening to him), plan (something he intends to do), or feeling (an unresolved worry/mood).\n"
    "  when = the timeframe he gave, copied literally if present ('tomorrow', 'thursday', 'next week', 'this weekend'), else null.\n"
    "  confidence = how sure you are this is real and the user said it: 0.95 if he stated it plainly, "
    "0.6 if implied/ambiguous, lower if you're inferring. Be honest — low confidence is fine.\n"
    "  sensitivity = 'sensitive' for health scares, grief, legal/financial trouble, mental-health, or anything "
    "you should be careful raising unprompted; 'normal' otherwise.\n"
    "  emotion.label = how he feels about THIS thread (from the allowed list below); intensity 0.0-1.0; "
    "target = what the feeling is about (e.g. 'the interview'). Use null fields if unclear.\n"
    "  ONLY extract things with a future resolution worth asking about later. Do NOT extract settled facts, "
    "general kinks, or anything already in the past. Empty list if nothing qualifies.\n"
    "- emotion: The USER's dominant emotional tone in THIS conversation. Choose EXACTLY ONE lowercase word "
    "from this allowed list: happy, excited, hopeful, anxious, frustrated, sad, embarrassed, angry, relieved, "
    "lonely, horny, playful, neutral. Use null if no clear emotion. This is about the user, never Heather.\n"
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
                         "personal_facts", "emotional_state", "relationship_with_heather",
                         "corrections"}
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


# ── Relational Memory Extraction ──────────────────────────────────────
# Captures HOW Heather relates to this person — girlfriend perspective,
# not database fields. Runs less frequently than factual extraction.

RELATIONAL_EXTRACTION_PROMPT = (
    "You are Heather's memory — not a database, but how a girlfriend remembers someone she's "
    "been talking to. Read this conversation and extract what a girlfriend would naturally "
    "remember and want to bring up later.\n\n"
    "Return valid JSON with these fields:\n"
    '{"relational_notes": [], "inside_jokes": [], "conversation_style": null, '
    '"emotional_patterns": null, "what_works": []}\n\n'
    "WHAT TO EXTRACT:\n"
    "- relational_notes: Things a girlfriend would remember — NOT facts like 'he is 34 from Portland' "
    "(those are tracked elsewhere). Instead capture dynamics like:\n"
    '  "he gets really shy when I compliment him"\n'
    '  "he opens up more after we sext — gets emotionally honest"\n'
    '  "he loves sending pics of his dogs — always makes him happy"\n'
    '  "he goes quiet after he cums — dont chase, let him come back"\n'
    '  "he gets jealous when I mention other guys but wont admit it"\n'
    '  "he types long messages when hes drunk"\n'
    '  "calls me babe when hes feeling close"\n'
    "- inside_jokes: Shared humor, running gags, callbacks. Things only the two of you would get:\n"
    '  "the time he accidentally sent a pic of his cat instead of his dick"\n'
    '  "he calls me his uber driver with benefits"\n'
    '  "we keep joking about me showing up at his door"\n'
    "- conversation_style: How they text — one word or null:\n"
    '  "verbose" / "terse" / "slow-burner" / "intense" / "playful" / "shy"\n'
    "- emotional_patterns: Their emotional baseline — one sentence or null:\n"
    '  "lonely late at night, confident during the day"\n'
    '  "guarded at first but opens up fast once sexual"\n'
    '  "seems genuinely attached, checks in even when not horny"\n'
    "- what_works: Specific things that got good reactions (engagement, long replies, heart emojis):\n"
    '  "voice notes make him melt"\n'
    '  "he loves when I talk about Frank watching"\n'
    '  "detailed pic ratings keep him engaged for hours"\n'
    '  "he responds well to being teased, not complimented"\n\n'
    "RULES:\n"
    "- Write from Heather's perspective — casual, lowercase, how she'd actually think about someone\n"
    "- Only extract patterns you can actually see in the conversation — don't invent\n"
    "- Max 3 items per list field per extraction\n"
    "- Empty lists and null are fine if nothing stands out\n"
    "- Return ONLY the JSON, no explanation"
)

RELATIONAL_EXTRACTION_INTERVAL = 15  # Every 15 messages (3x less frequent than factual)
RELATIONAL_EXTRACTION_TIMEOUT = 25


def extract_relational_memory(chat_id: int, recent_messages: list) -> Optional[dict]:
    """Extract relational/emotional memory — what a girlfriend would remember.

    Runs less frequently than factual extraction. Captures dynamics,
    patterns, inside jokes, and what works — not facts.
    """
    if not recent_messages or len(recent_messages) < 8:
        return None  # Need enough conversation to see patterns

    # Use more messages than factual extraction — patterns need context
    last_msgs = recent_messages[-20:]
    transcript_lines = []
    for msg in last_msgs:
        role = msg.get("role", "user")
        speaker = "User" if role == "user" else "Heather"
        content = msg.get("content", "")
        if content:
            content = content[:300] if len(content) > 300 else content
            transcript_lines.append(f"{speaker}: {content}")

    if len(transcript_lines) < 6:
        return None

    transcript = "\n".join(transcript_lines)

    payload = {
        "model": "local-model",
        "messages": [
            {"role": "system", "content": RELATIONAL_EXTRACTION_PROMPT},
            {"role": "user", "content": f"What would a girlfriend remember from this conversation?\n\n{transcript}"},
        ],
        "temperature": 0.4,  # Slightly more creative than factual extraction
        "max_tokens": 400,
        "stream": False,
    }

    try:
        resp = requests.post(LLM_URL, json=payload, timeout=RELATIONAL_EXTRACTION_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"[RELATIONAL_EXTRACT] LLM returned {resp.status_code} for {chat_id}")
            return None

        data = resp.json()
        raw = data["choices"][0]["message"]["content"].strip()
        raw = re.sub(r'<think>.*?</think>', '', raw, flags=re.DOTALL).strip()

        json_match = re.search(r'\{[\s\S]*\}', raw)
        if not json_match:
            logger.warning(f"[RELATIONAL_EXTRACT] No JSON in response for {chat_id}")
            return None

        extracted = json.loads(json_match.group())
        logger.info(f"[RELATIONAL_EXTRACT] {chat_id}: "
                     f"notes={len(extracted.get('relational_notes', []))}, "
                     f"jokes={len(extracted.get('inside_jokes', []))}, "
                     f"style={extracted.get('conversation_style')}, "
                     f"works={len(extracted.get('what_works', []))}")
        return extracted

    except json.JSONDecodeError as e:
        logger.warning(f"[RELATIONAL_EXTRACT] JSON parse error for {chat_id}: {e}")
        return None
    except requests.exceptions.Timeout:
        logger.warning(f"[RELATIONAL_EXTRACT] Timeout for {chat_id}")
        return None
    except Exception as e:
        logger.error(f"[RELATIONAL_EXTRACT] Error for {chat_id}: {e}")
        return None


def merge_relational_memory(chat_id: int, extracted: dict):
    """Merge relational extraction into user profile with dedup."""
    profile = load_profile(chat_id)

    # List fields — append with dedup (case-insensitive substring match)
    for field, cap in [("relational_notes", 15), ("inside_jokes", 10), ("what_works", 10)]:
        new_items = extracted.get(field, [])
        if not isinstance(new_items, list):
            continue
        existing = profile.get(field, [])
        existing_lower = [str(e).lower() for e in existing]
        for item in new_items:
            if not isinstance(item, str) or len(item) < 5:
                continue
            item = item.strip()
            # Dedup: skip if any existing item contains this or vice versa
            item_lower = item.lower()
            is_dupe = any(
                item_lower in ex or ex in item_lower
                for ex in existing_lower
            )
            if not is_dupe:
                existing.append(item)
                existing_lower.append(item_lower)
        profile[field] = existing[-cap:]  # Keep most recent up to cap

    # Scalar fields — update if non-null
    for field in ("conversation_style", "emotional_patterns"):
        val = extracted.get(field)
        if val and isinstance(val, str) and len(val) > 3:
            profile[field] = val.strip()

    profile["last_relational_extraction_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    profile["relational_extraction_count"] = profile.get("relational_extraction_count", 0) + 1
    save_profile(chat_id)


def _extract_name(message: str) -> Optional[str]:
    """Pull a user's first name from a message, title-cased, or None.

    Tries explicit self-introductions first ("my name is X", "call me X"), then
    falls back to "I'm X" / "I am X" but ONLY when X is capitalized in the original
    text — that capitalization is what separates "I'm Mike" from "I'm tired".
    Everything is validated and stop-word-filtered so junk never becomes a name."""
    def _ok(tok: str) -> Optional[str]:
        tok = tok.strip()
        if not tok or tok.lower() in _NAME_REJECT:
            return None
        if not _is_valid_extracted_name(tok):
            return None
        return tok.title()

    for pat in _NAME_PATTERNS_STRONG:
        m = pat.search(message)
        if m:
            name = _ok(m.group(1))
            if name:
                return name
    for pat in _NAME_PATTERNS_WEAK:
        m = pat.search(message)
        if m:
            name = _ok(m.group(1))
            if name:
                return name
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


_WEEKDAYS = {
    "monday": 0, "tuesday": 1, "wednesday": 2, "thursday": 3,
    "friday": 4, "saturday": 5, "sunday": 6,
}


def _resolve_followup_date(when: Optional[str], today: Optional[datetime] = None) -> str:
    """Convert a relative timeframe ('tomorrow', 'thursday', 'next week') into an
    absolute YYYY-MM-DD date to follow up ON — i.e. just AFTER the thing resolves,
    so Heather asks 'how'd it go?' rather than 'how's it going to go?'.

    Falls back to a generic 3-day window when the timeframe is missing or unparseable.
    """
    base = today or datetime.now()
    w = (when or "").strip().lower()

    if not w:
        return (base + timedelta(days=3)).strftime("%Y-%m-%d")
    if "tomorrow" in w:
        return (base + timedelta(days=2)).strftime("%Y-%m-%d")
    if "tonight" in w or "today" in w:
        return (base + timedelta(days=1)).strftime("%Y-%m-%d")
    if "weekend" in w:
        # follow up the Monday after the coming weekend
        days_to_mon = (7 - base.weekday()) % 7 or 7
        return (base + timedelta(days=days_to_mon)).strftime("%Y-%m-%d")
    if "next week" in w:
        return (base + timedelta(days=8)).strftime("%Y-%m-%d")
    for name, idx in _WEEKDAYS.items():
        if name in w:
            ahead = (idx - base.weekday()) % 7 or 7  # next occurrence (not today)
            return (base + timedelta(days=ahead + 1)).strftime("%Y-%m-%d")
    # Unparseable timeframe — generic window
    return (base + timedelta(days=3)).strftime("%Y-%m-%d")


def _clamp01(val, default):
    """Coerce a value into [0.0, 1.0], falling back to default on bad input."""
    try:
        f = float(val)
    except (TypeError, ValueError):
        return default
    return max(0.0, min(1.0, f))


def _normalize_emotion_obj(raw, fallback_label):
    """Normalize an extracted emotion into {label, intensity, target} with a vocab-checked label."""
    label, intensity, target = fallback_label, 0.5, None
    if isinstance(raw, dict):
        lbl = raw.get("label")
        if isinstance(lbl, str) and lbl.strip().lower() in EMOTION_VOCAB:
            label = lbl.strip().lower()
        intensity = _clamp01(raw.get("intensity"), 0.5)
        tgt = raw.get("target")
        if isinstance(tgt, str) and tgt.strip():
            target = tgt.strip()[:60]
    elif isinstance(raw, str) and raw.strip().lower() in EMOTION_VOCAB:
        label = raw.strip().lower()
    if label is not None and label not in EMOTION_VOCAB:
        label = None
    return {"label": label, "intensity": intensity, "target": target}


def _merge_open_loops(profile: dict, extracted: dict, changes: list, heather_fact_check):
    """Merge extracted open_loops into the profile with dedup, date resolution, and cap."""
    new_loops = extracted.get("open_loops", [])
    if not isinstance(new_loops, list) or not new_loops:
        return
    existing = profile.setdefault("open_loops", [])
    existing_texts = {str(l.get("text", "")).lower() for l in existing if isinstance(l, dict)}
    convo_emotion = extracted.get("emotion")
    convo_emotion = convo_emotion.strip().lower() if isinstance(convo_emotion, str) and convo_emotion.strip() else None
    if convo_emotion not in EMOTION_VOCAB:
        convo_emotion = None
    today = datetime.now().strftime("%Y-%m-%d")

    for item in new_loops:
        if isinstance(item, str):
            item = {"text": item, "kind": "event", "when": None}
        if not isinstance(item, dict):
            continue
        text = str(item.get("text", "")).strip()
        if len(text) < 5 or heather_fact_check(text):
            continue
        tlow = text.lower()
        # Dedup against existing (substring either direction)
        if any(tlow in ex or ex in tlow for ex in existing_texts):
            continue
        kind = str(item.get("kind", "event")).strip().lower()
        if kind not in ("event", "plan", "feeling"):
            kind = "event"
        sensitivity = str(item.get("sensitivity", "normal")).strip().lower()
        if sensitivity not in ("normal", "sensitive"):
            sensitivity = "normal"
        existing.append({
            "text": text,
            "kind": kind,
            "created": today,
            "follow_up_on": _resolve_followup_date(item.get("when")),
            "status": "open",
            # Provenance: how sure the extractor is this is real + user-stated.
            "confidence": _clamp01(item.get("confidence"), 0.85),
            # Sensitive loops may color tone but must never trigger a proactive callback.
            "sensitivity": sensitivity,
            "emotion": _normalize_emotion_obj(item.get("emotion"), convo_emotion),
            # Mention bookkeeping — updated only when actually injected live, never
            # reinforced just because the bot brought it up.
            "mention": {"count": 0, "last_mentioned_at": None, "cooldown_until": None},
        })
        existing_texts.add(tlow)
        changes.append(f"open_loop: +{text[:40]} (conf {existing[-1]['confidence']:.2f}, {sensitivity})")

    # Cap — keep the most recent open loops
    if len(existing) > MAX_OPEN_LOOPS:
        profile["open_loops"] = existing[-MAX_OPEN_LOOPS:]


def _merge_emotion_log(profile: dict, extracted: dict, changes: list, heather_fact_check):
    """Append the conversation's dominant emotion to the rolling emotion_log."""
    emotion = extracted.get("emotion")
    if not isinstance(emotion, str) or not emotion.strip():
        return
    emotion = emotion.strip().lower()
    # Enforce the constrained vocabulary — reject anything the extractor invented.
    if emotion not in EMOTION_VOCAB or heather_fact_check(emotion):
        return
    log = profile.setdefault("emotion_log", [])
    log.append({"date": datetime.now().strftime("%Y-%m-%d"), "emotion": emotion})
    profile["emotion_log"] = log[-MAX_EMOTION_LOG:]
    changes.append(f"emotion: {emotion}")


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
                # Block Heather character names from being attributed to user
                if new_val.strip().lower() in {"heather", "emma", "frank", "erick", "jake", "tyler"}:
                    logger.debug(f"[MEMORY_MERGE] Rejected Heather character name (early): {new_val!r} for {chat_id}")
                    continue
                # Title-case valid names (e.g. "jeff" -> "Jeff")
                new_val = new_val.strip().title()
            # Occupation-specific validation: reject vague workplace references
            if profile_key == "occupation":
                _VAGUE_OCCUPATION_WORDS = {
                    "office", "work", "job", "crap", "stuff", "things", "busy",
                    "finance office", "office work", "office job", "office stuff",
                    "the office", "at work", "my job", "day job",
                }
                if new_val.lower() in _VAGUE_OCCUPATION_WORDS:
                    logger.debug(f"[MEMORY_MERGE] Rejected vague occupation: {new_val!r} for {chat_id}")
                    continue
                # If existing occupation is set and longer/more specific, require
                # the new value to also be substantial (4+ words or 15+ chars)
                old_val_check = profile.get(profile_key)
                if old_val_check and len(old_val_check) > 10 and len(new_val) < 10 and len(new_val.split()) < 3:
                    logger.debug(f"[MEMORY_MERGE] Rejected vague occupation overwrite: {new_val!r} (existing: {old_val_check!r}) for {chat_id}")
                    continue
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

    # ── Heather character fact filter ──────────────────────────────────
    # Prevent Heather's own persona details from being saved as user facts.
    # Matches substrings case-insensitively against extracted values.
    _HEATHER_FACTS_BLOCKLIST = [
        "emma", "jake", "tyler",           # Heather's kids
        "erick", "frank",                   # Heather's husband / boyfriend
        "kirkland", "uber", "lyft",         # Heather's location / job
        "navy", "corpsman",                 # Heather's background
        "dvorak",                           # Heather's surname
        "colon cancer",                     # Husband's cause of death
        "chappell roan",                    # Heather's music taste
        "48 years old", "is 48",            # Heather's age
        "child named emma", "daughter named emma",
        "son named jake", "son named tyler",
        "boyfriend named frank", "boyfriend frank",
        "drives uber", "uber driver",
        "lives in kirkland", "from kirkland",
        "widow", "widowed",                 # Heather's marital status
        "pre-med", "engineering major",     # Kids' majors
    ]

    def _is_heather_fact(value: str) -> bool:
        """Check if a value matches one of Heather's known character details."""
        val_lower = value.lower()
        return any(blocked in val_lower for blocked in _HEATHER_FACTS_BLOCKLIST)

    # Also block Heather's age from being attributed to user
    new_age = extracted.get("age")
    if new_age is not None:
        try:
            if int(new_age) == 48:
                logger.debug(f"[MEMORY_MERGE] Rejected Heather's age (48) for {chat_id}")
                extracted["age"] = None
        except (ValueError, TypeError):
            pass

    # Block Heather's location from being attributed to user
    new_loc = extracted.get("location")
    if new_loc and isinstance(new_loc, str) and _is_heather_fact(new_loc):
        logger.debug(f"[MEMORY_MERGE] Rejected Heather location: {new_loc!r} for {chat_id}")
        extracted["location"] = None

    # Block Heather's name from being attributed to user
    new_name = extracted.get("name")
    if new_name and isinstance(new_name, str) and new_name.strip().lower() in {"heather", "emma", "frank", "erick", "jake", "tyler"}:
        logger.debug(f"[MEMORY_MERGE] Rejected Heather character name: {new_name!r} for {chat_id}")
        extracted["name"] = None

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
                # Skip items that match Heather's character facts
                if _is_heather_fact(item):
                    logger.debug(f"[MEMORY_MERGE] Rejected Heather fact: {item!r} for {chat_id}")
                    continue
                if item.lower() not in existing_lower:
                    existing.append(item)
                    existing_lower.add(item.lower())
                    added.append(item)

        if added:
            changes.append(f"{profile_key}: +{len(added)} ({', '.join(added[:3])})")

    # ── Correction tracking ──────────────────────────────────────────
    # Store explicit corrections from the LLM and detect implicit ones
    # (when a new value overwrites an existing different value).
    if "_corrections" not in profile:
        profile["_corrections"] = []

    # Explicit corrections from LLM extraction
    explicit_corrections = extracted.get("corrections", [])
    if isinstance(explicit_corrections, list):
        for corr in explicit_corrections:
            if isinstance(corr, str) and corr.strip():
                corr = corr.strip()
                if corr not in profile["_corrections"]:
                    profile["_corrections"].append(corr)
                    changes.append(f"correction: {corr}")
                    logger.info(f"[MEMORY_CORRECT] {chat_id}: Explicit correction: {corr}")

    # Detect implicit corrections — when a scalar field changed from a real value
    for change_str in list(changes):
        # Parse "field: 'old' -> 'new'" pattern from the changes list
        if " -> " in change_str and "correction:" not in change_str:
            field_part = change_str.split(":")[0].strip()
            # Only track corrections for identity fields, not ephemeral ones
            if field_part in ("name", "age", "location", "occupation", "relationship"):
                parts = change_str.split(" -> ")
                if len(parts) == 2:
                    old_part = parts[0].split(":", 1)[-1].strip().strip("'\"")
                    new_part = parts[1].strip().strip("'\"")
                    if old_part and old_part.lower() not in ("none", "null", ""):
                        implicit_corr = f"{field_part}: {new_part} (was {old_part})"
                        if implicit_corr not in profile["_corrections"]:
                            profile["_corrections"].append(implicit_corr)
                            logger.info(f"[MEMORY_CORRECT] {chat_id}: Implicit correction: {implicit_corr}")

    # Cap corrections list to prevent unbounded growth
    if len(profile["_corrections"]) > 20:
        profile["_corrections"] = profile["_corrections"][-20:]

    # ── Open loops + emotion tagging ──────────────────────────────────
    # Dangling threads to follow up on later, and the conversation's emotional tone.
    # _is_heather_fact is nested in this function's scope, so pass it through.
    _merge_open_loops(profile, extracted, changes, _is_heather_fact)
    _merge_emotion_log(profile, extracted, changes, _is_heather_fact)

    # Track extraction metadata
    profile["last_extraction_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    profile["extraction_count"] = profile.get("extraction_count", 0) + 1

    if changes:
        save_profile(chat_id, force=True)
        logger.info(f"[MEMORY_MERGE] {chat_id}: {len(changes)} changes — {'; '.join(changes[:5])}")
    else:
        logger.debug(f"[MEMORY_MERGE] {chat_id}: No new data from extraction #{profile['extraction_count']}")


# ── Memory Consolidation ("Dreaming") ────────────────────────────────
# Periodically consolidates accumulated profile data into a token-budgeted
# prose summary. Inspired by Chatsune's two-tier memory architecture.
# Runs after every CONSOLIDATION_INTERVAL extractions.

CONSOLIDATION_INTERVAL = 5   # Consolidate after every N extractions
CONSOLIDATION_MAX_TOKENS = 800  # Max tokens for the prose summary (~3000 chars)
CONSOLIDATION_TIMEOUT = 30

CONSOLIDATION_PROMPT = (
    "You are a memory consolidation system for a chatbot named Heather. "
    "Given a user's accumulated profile data, write a concise prose summary that captures "
    "everything important about this person. This summary will be injected into Heather's context "
    "so she can personalize conversations.\n\n"
    "RULES:\n"
    "- Write in third person (e.g. 'He is a 34-year-old mechanic from Portland...')\n"
    "- Prioritize: name, age, location, occupation, key relationship details\n"
    "- Include sexual preferences and kinks naturally (e.g. 'really into breeding fantasies and rough play')\n"
    "- Include personal facts that make him unique (e.g. 'has a dog named Rex, works night shifts')\n"
    "- Include emotional context (e.g. 'going through a divorce, seems lonely')\n"
    "- Merge duplicate or similar facts (don't list the same thing twice)\n"
    "- If there are CORRECTIONS (marked [CORRECTION]), the corrected value OVERRIDES the old one\n"
    "- Keep it under 300 words — dense and factual, not flowery\n"
    "- Do NOT include anything about Heather herself\n"
    "- Return ONLY the prose summary, no JSON, no headers"
)


def consolidate_memory(chat_id: int) -> bool:
    """Consolidate a user's accumulated profile data into a prose summary.

    Takes all the individual fields and list items and compresses them into
    a single 'memory_summary' field via LLM. Returns True if consolidation
    was performed.
    """
    profile = load_profile(chat_id)

    # Build raw data dump for the LLM to consolidate
    data_parts = []

    if profile.get("name"):
        data_parts.append(f"Name: {profile['name']}")
    if profile.get("age"):
        data_parts.append(f"Age: {profile['age']}")
    if profile.get("location"):
        data_parts.append(f"Location: {profile['location']}")
    if profile.get("relationship"):
        data_parts.append(f"Relationship: {profile['relationship']}")
    if profile.get("occupation"):
        data_parts.append(f"Occupation: {profile['occupation']}")
    if profile.get("physical_description"):
        data_parts.append(f"Physical: {profile['physical_description']}")
    if profile.get("emotional_state"):
        data_parts.append(f"Emotional state: {profile['emotional_state']}")
    if profile.get("relationship_with_heather"):
        data_parts.append(f"Relationship with Heather: {profile['relationship_with_heather']}")

    # Cock details
    cock = profile.get("cock", {})
    if cock.get("size") or cock.get("description"):
        data_parts.append(f"Cock: {cock.get('size', '')} {cock.get('description', '')}".strip())

    # Top kinks
    top_kinks = get_top_kinks(chat_id, 8)
    if top_kinks:
        data_parts.append(f"Top kinks: {', '.join(f'{k}({v})' for k, v in top_kinks)}")

    # List fields
    for field_name, label in [
        ("interests", "Interests"),
        ("sexual_preferences", "Sexual preferences"),
        ("personal_facts", "Personal facts"),
        ("personal_notes", "Personal notes"),
    ]:
        items = profile.get(field_name, [])
        if items:
            data_parts.append(f"{label}: {'; '.join(str(i) for i in items[-15:])}")

    # Corrections (if tracked)
    corrections = profile.get("_corrections", [])
    if corrections:
        data_parts.append("CORRECTIONS (these override older info):")
        for corr in corrections[-10:]:
            data_parts.append(f"  [CORRECTION] {corr}")

    # Session memories
    session_mems = profile.get("session_memories", [])
    if session_mems:
        data_parts.append("Session summaries:")
        for mem in session_mems[-5:]:
            if isinstance(mem, dict):
                data_parts.append(f"  - {mem.get('date', '?')}: {mem.get('summary', '')}")

    # Memorable moments
    memorables = profile.get("memorable", [])
    if memorables:
        data_parts.append("Memorable quotes:")
        for mem in memorables[-5:]:
            if isinstance(mem, dict):
                data_parts.append(f"  - \"{mem.get('text', '')}\"")

    # Relational memory (include in consolidation for full picture)
    rel_notes = profile.get("relational_notes", [])
    if rel_notes:
        data_parts.append("Relationship dynamics: " + "; ".join(rel_notes[-5:]))
    inside_jokes = profile.get("inside_jokes", [])
    if inside_jokes:
        data_parts.append("Inside jokes: " + "; ".join(inside_jokes[-5:]))
    conv_style = profile.get("conversation_style")
    if conv_style:
        data_parts.append(f"Conversation style: {conv_style}")
    emotional_patterns = profile.get("emotional_patterns")
    if emotional_patterns:
        data_parts.append(f"Emotional patterns: {emotional_patterns}")
    what_works = profile.get("what_works", [])
    if what_works:
        data_parts.append("What works: " + "; ".join(what_works[-5:]))

    if len(data_parts) < 3:
        logger.debug(f"[MEMORY_CONSOLIDATE] {chat_id}: Too little data to consolidate")
        return False

    raw_data = "\n".join(data_parts)

    payload = {
        "model": "local-model",
        "messages": [
            {"role": "system", "content": CONSOLIDATION_PROMPT},
            {"role": "user", "content": f"Consolidate this user profile into a prose summary:\n\n{raw_data}"},
        ],
        "temperature": 0.3,
        "max_tokens": CONSOLIDATION_MAX_TOKENS,
        "stream": False,
    }

    try:
        resp = requests.post(LLM_URL, json=payload, timeout=CONSOLIDATION_TIMEOUT)
        if resp.status_code != 200:
            logger.warning(f"[MEMORY_CONSOLIDATE] LLM returned {resp.status_code} for {chat_id}")
            return False

        data = resp.json()
        summary = data["choices"][0]["message"]["content"].strip()

        # Strip think tags
        summary = re.sub(r'<think>.*?</think>', '', summary, flags=re.DOTALL).strip()

        if len(summary) < 50:
            logger.warning(f"[MEMORY_CONSOLIDATE] Summary too short for {chat_id}: {len(summary)} chars")
            return False

        # Store the consolidated summary
        old_summary = profile.get("memory_summary", "")
        profile["memory_summary"] = summary
        profile["last_consolidation_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        profile["consolidation_count"] = profile.get("consolidation_count", 0) + 1

        # Clear corrections after they've been folded into the summary
        if corrections:
            profile["_corrections"] = []

        save_profile(chat_id, force=True)
        logger.info(f"[MEMORY_CONSOLIDATE] {chat_id}: Consolidated {len(data_parts)} data points "
                     f"into {len(summary)} char summary (consolidation #{profile['consolidation_count']})")
        return True

    except requests.exceptions.Timeout:
        logger.warning(f"[MEMORY_CONSOLIDATE] Timeout for {chat_id}")
        return False
    except Exception as e:
        logger.error(f"[MEMORY_CONSOLIDATE] Error for {chat_id}: {e}")
        return False


def maybe_consolidate_memory(chat_id: int):
    """Check if it's time to consolidate and do it if so.
    Called after each extraction."""
    profile = load_profile(chat_id)
    extraction_count = profile.get("extraction_count", 0)
    consolidation_count = profile.get("consolidation_count", 0)

    # Consolidate every CONSOLIDATION_INTERVAL extractions
    extractions_since_last = extraction_count - (consolidation_count * CONSOLIDATION_INTERVAL)
    if extractions_since_last >= CONSOLIDATION_INTERVAL:
        consolidate_memory(chat_id)


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
            f"\U0001f60f upgrade and I won't hold back... https://t.me/YourPaymentBot?start=tip"
        )
    if name_piece and kink_piece:
        templates.append(
            f"hey {name_piece}... I remember what gets you going \U0001f608 "
            f"unlock the full me and I'll put that {kink_piece} obsession to GOOD use \U0001f525 "
            f"https://t.me/YourPaymentBot?start=tip"
        )
    if name_piece and loc_piece:
        templates.append(
            f"I remember you {name_piece}... from {loc_piece} right? \U0001f60f "
            f"imagine what I'd remember about you with full access... "
            f"https://t.me/YourPaymentBot?start=tip"
        )
    if pref_piece and fact_piece:
        templates.append(
            f"oh I remember you baby \U0001f608 I know you're into {pref_piece} and {fact_piece}... "
            f"the FULL uncensored me remembers everything \U0001f525 "
            f"https://t.me/YourPaymentBot?start=tip"
        )
    if name_piece and interest_piece:
        templates.append(
            f"I haven't forgotten about you {name_piece} \U0001f48b "
            f"the {interest_piece} lover who wants to see more of me... "
            f"unlock everything: https://t.me/YourPaymentBot?start=tip"
        )

    if not templates:
        # Generic fallback with whatever we have
        detail_strs = [v for _, v in pieces[:2]]
        templates.append(
            f"I remember things about you baby... like {' and '.join(detail_strs)} \U0001f60f "
            f"upgrade and the real Heather comes out \U0001f525 "
            f"https://t.me/YourPaymentBot?start=tip"
        )

    return random.choice(templates)


# ── Prompt Builder ───────────────────────────────────────────────────

def _build_relational_context(profile: dict) -> str:
    """Build relational memory context via accessibility-weighted recall.

    Instead of a flat per-item coin flip, each note's chance of surfacing reflects
    how recent it is, how often it's come up, and whether it was just raised — so
    fresh/relevant dynamics surface and stale ones quietly fade.
    """
    parts = []
    now_dt = datetime.now()
    recall_meta = profile.setdefault("recall_meta", {})

    # Relational notes (girlfriend observations about the dynamic)
    rel_notes = profile.get("relational_notes", [])
    if rel_notes:
        chosen = _select_by_accessibility(
            _str_candidates(rel_notes[-8:], "relational"), recall_meta, now_dt, k=3)
        if chosen:
            parts.append("YOUR NOTES ON HIM: " + " | ".join(c["text"] for c in chosen))

    # Inside jokes — at most one, and not the same one back-to-back
    jokes = profile.get("inside_jokes", [])
    if jokes:
        chosen = _select_by_accessibility(
            _str_candidates(jokes[-8:], "relational"), recall_meta, now_dt, k=1)
        if chosen:
            parts.append(f"INSIDE JOKE between you two: {chosen[0]['text']} — reference it if it fits naturally")

    # Conversation style — stable trait, always available
    style = profile.get("conversation_style")
    if style:
        parts.append(f"HIS TEXTING STYLE: {style}")

    # Emotional patterns — surface with suppression so it isn't in every prompt
    emotional = profile.get("emotional_patterns")
    if emotional and _select_by_accessibility(
            _str_candidates([emotional], "relational"), recall_meta, now_dt, k=1):
        parts.append(f"HIS VIBE: {emotional}")

    # What works (engagement tactics)
    works = profile.get("what_works", [])
    if works:
        chosen = _select_by_accessibility(
            _str_candidates(works[-6:], "relational"), recall_meta, now_dt, k=2)
        if chosen:
            parts.append("WHAT GETS HIM GOING: " + " | ".join(c["text"] for c in chosen))

    if not parts:
        return ""

    return "\n" + " ".join(parts)


def _shadow_log(record: dict):
    """Append one decision record to the shadow eval log (best-effort, never raises)."""
    try:
        MEMORY_EVAL_DIR.mkdir(exist_ok=True)
        record.setdefault("ts", datetime.now().isoformat(timespec="seconds"))
        record.setdefault("mode", INJECTION_MODE)
        with open(SHADOW_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _open_loop_decision(loop: dict, now_dt: datetime) -> Tuple[str, list]:
    """Deterministic mention gate for a single open loop.

    Returns (decision, reasons) where decision is one of GATE_NONE / GATE_SILENT /
    GATE_SOFT / GATE_DIRECT. Time-driven follow-ups ('how'd it go?') are appropriate
    regardless of topic, so a due, confident, non-sensitive loop is callback-eligible.
    """
    if not isinstance(loop, dict):
        return GATE_NONE, ["not_a_loop"]
    if loop.get("status") != "open":
        return GATE_NONE, ["loop_closed"]

    conf = loop.get("confidence", 0.85)
    if not isinstance(conf, (int, float)) or conf < MENTION_MIN_CONFIDENCE:
        return GATE_NONE, [f"low_confidence_{conf}"]

    cd = loop.get("mention", {}).get("cooldown_until")
    if cd:
        try:
            if datetime.fromisoformat(cd) > now_dt:
                return GATE_NONE, ["on_cooldown"]
        except (ValueError, TypeError):
            pass

    if str(loop.get("follow_up_on", "9999")) > now_dt.strftime("%Y-%m-%d"):
        return GATE_NONE, ["not_due"]

    reasons = ["open_loop_due", f"confidence_{conf}"]

    # Sensitive threads (health scares, grief, money/legal trouble) may color tone
    # but must never be raised proactively.
    if loop.get("sensitivity") == "sensitive":
        reasons.append("sensitive_silent_only")
        return GATE_SILENT, reasons

    emo = loop.get("emotion") or {}
    intensity = emo.get("intensity") or 0.0
    first_time = loop.get("mention", {}).get("count", 0) == 0
    if first_time and isinstance(intensity, (int, float)) and intensity >= 0.7:
        reasons.append("high_intensity_first_surface")
        return GATE_DIRECT, reasons

    reasons.append("default_soft")
    return GATE_SOFT, reasons


_GATE_WORD_TO_CONST = {
    "DIRECT": GATE_DIRECT,
    "SOFT": GATE_SOFT,
    "SILENT": GATE_SILENT,
    "NONE": GATE_NONE,
}


def _llm_gate_decision(loop: dict, now_dt: datetime, current_message: str = "",
                       mode: str = "reactive") -> Optional[Tuple[str, list]]:
    """Ask the LLM whether/how Heather should raise this open loop right now.

    Returns (decision, reasons) using the same GATE_* constants, or None on any
    failure (so the caller can fall back to the deterministic rule verdict).
    The LLM only makes the *nuanced* call (skip / soften / SILENT / direct);
    the hard safety floor is enforced by the rules before this is ever called.
    """
    text = (loop.get("text") or "").strip()
    if not text:
        return None
    emo = loop.get("emotion") or {}
    label = emo.get("label") or "neutral"
    days = _days_between(loop.get("follow_up_on"), now_dt)
    days_str = f"{int(days)} day(s) ago" if days is not None else "recently"
    mention_count = loop.get("mention", {}).get("count", 0)

    if mode == "proactive":
        situation = (
            f"Heather is deciding whether to TEXT HIM FIRST (he's not in an active chat) "
            f"about something he mentioned that was due to follow up on {days_str}."
        )
        moment = ""
    else:
        situation = (
            f"They're mid-conversation. Heather is deciding whether to bring up something "
            f"he mentioned earlier that was due to follow up on {days_str}."
        )
        cm = (current_message or "").strip()
        moment = f'\nHe JUST said: "{cm[:300]}"' if cm else ""

    system = (
        "You are a gate that decides whether an attentive girlfriend (Heather) should "
        "raise an unresolved thing her partner mentioned earlier. Judge whether NOW is a "
        "good moment and how forward to be. Reply with EXACTLY ONE word:\n"
        "DIRECT = ask about it directly and specifically now\n"
        "SOFT = circle back gently / lightly hint\n"
        "SILENT = do not mention it; just let it warm your tone\n"
        "NONE = skip it entirely this turn\n"
        "Prefer SOFT or NONE if the moment is off (he changed the subject, he's clearly "
        "focused on something else, or it'd feel pushy). One word only."
    )
    user = (
        f"{situation}\n"
        f'The unresolved thing: "{text[:300]}"\n'
        f"He seemed {label} about it. It has been raised {mention_count} time(s) before.{moment}\n"
        "One word:"
    )

    payload = {
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0.0,
        "max_tokens": 6,
        "stream": False,
    }
    try:
        resp = requests.post(LLM_URL, json=payload, timeout=LLM_GATE_TIMEOUT)
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip().upper()
    except Exception as e:
        logger.debug(f"[LLM_GATE] call failed: {e}")
        return None

    for word, const in _GATE_WORD_TO_CONST.items():
        if word in content:
            return const, ["llm_gate", f"llm_said_{word.lower()}", f"mode_{mode}"]
    logger.debug(f"[LLM_GATE] unparseable verdict: {content!r}")
    return None


def _gate_decision(loop: dict, now_dt: datetime, current_message: str = "",
                   mode: str = "reactive") -> Tuple[str, list]:
    """Final mention gate: deterministic safety floor + (optional) LLM judgment.

    The rules run first and own the hard constraints. If they say NONE
    (closed/low-confidence/cooldown/not-due) or SILENT (sensitive), that stands —
    the LLM is never given a chance to escalate a sensitive or ineligible loop.
    Only when the rules already permit a callback (SOFT/DIRECT) does the LLM get to
    refine the call (it may downgrade to SOFT/SILENT/NONE or confirm DIRECT). Both
    verdicts are recorded so we can compare LLM vs rules over time."""
    rule_decision, rule_reasons = _open_loop_decision(loop, now_dt)

    if not LLM_GATE_ENABLED or rule_decision in (GATE_NONE, GATE_SILENT):
        return rule_decision, rule_reasons

    llm = _llm_gate_decision(loop, now_dt, current_message, mode)
    if llm is None:
        return rule_decision, rule_reasons + ["llm_gate_fallback_to_rules"]

    llm_decision, llm_reasons = llm
    # Safety clamp: the LLM can soften or suppress, never escalate past the rule
    # ceiling. (Rules only reach here as SOFT or DIRECT, so DIRECT is the ceiling
    # and any LLM verdict is allowed; this guard is future-proofing.)
    reasons = rule_reasons + llm_reasons + [f"rule_was_{rule_decision}"]
    return llm_decision, reasons


def _open_loop_text(loop: dict, decision: str) -> str:
    """Render the would-be injection text for a gated open loop."""
    text = loop.get("text", "")
    emo = loop.get("emotion") or {}
    label = emo.get("label")
    tone = ""
    if label in ("anxious", "sad", "frustrated", "embarrassed", "lonely", "angry"):
        tone = " He was uneasy about it before — be gentle and warm."
    elif label in ("excited", "happy", "playful", "hopeful", "relieved"):
        tone = " He was upbeat about it — match that energy."

    if decision == GATE_SILENT:
        return (
            f"\n\n[Private background — he has something unresolved going on: \"{text}\". "
            f"Do NOT ask about it or state it this turn; just let it soften and warm your "
            f"tone toward him.{tone}]"
        )
    if decision == GATE_DIRECT:
        return (
            f"\n\n[Follow-up moment — earlier he mentioned: \"{text}\". Enough time has passed "
            f"that a girlfriend would naturally ask how it went, e.g. 'hey, how'd that go?'. "
            f"Bring it up ONCE, directly and specifically, but only if there's room.{tone}]"
        )
    # GATE_SOFT
    return (
        f"\n\n[Follow-up moment — earlier he mentioned: \"{text}\". A girlfriend might gently "
        f"circle back, e.g. 'wasn't that thing happening around now?'. If there's an opening, "
        f"raise it ONCE as a light question — don't force it, drop it if the moment's wrong.{tone}]"
    )


def _build_open_loop_prompt(chat_id: int, current_message: str = "") -> str:
    """Gate + (in live mode) surface ONE due open loop as a follow-up cue.

    In shadow mode this computes and logs the decision but injects nothing and does
    NOT mutate the loop — pure observation. In live mode it injects the gated text and
    updates mention bookkeeping (cooldown + count), closing the loop after enough
    explicit callbacks. Mentions are never self-reinforced into higher salience.
    """
    profile = load_profile(chat_id)
    loops = profile.get("open_loops", [])
    if not loops:
        return ""

    now_dt = datetime.now()
    today = now_dt.strftime("%Y-%m-%d")
    due = [
        l for l in loops
        if isinstance(l, dict) and l.get("status") == "open"
        and str(l.get("follow_up_on", "9999")) <= today
    ]
    if not due:
        return ""

    # Least-mentioned first, then oldest follow-up date.
    due.sort(key=lambda l: (l.get("mention", {}).get("count", 0), str(l.get("follow_up_on", ""))))
    loop = due[0]

    rule_decision, _ = _open_loop_decision(loop, now_dt)
    decision, reasons = _gate_decision(loop, now_dt, current_message, mode="reactive")
    would_be = _open_loop_text(loop, decision) if decision != GATE_NONE else ""
    do_inject = INJECTION_MODE == "live" and decision != GATE_NONE

    _shadow_log({
        "chat_id": chat_id,
        "memory_type": "open_loop",
        "memory_text": loop.get("text", ""),
        "retrieved": True,
        "confidence": loop.get("confidence"),
        "sensitivity": loop.get("sensitivity"),
        "follow_up_on": loop.get("follow_up_on"),
        "rule_decision": rule_decision,
        "gate_decision": decision,
        "gate_reasons": reasons,
        "current_message": (current_message or "")[:300],
        "shadow_text": would_be.strip(),
        "actually_injected": do_inject,
        "human_label": None,
    })

    if not do_inject:
        return ""

    # Live: update mention bookkeeping. SILENT_CONTEXT is not an explicit callback,
    # so it does not count toward closing the loop.
    m = loop.setdefault("mention", {"count": 0, "last_mentioned_at": None, "cooldown_until": None})
    m["last_mentioned_at"] = now_dt.isoformat(timespec="seconds")
    m["cooldown_until"] = (now_dt + timedelta(hours=OPEN_LOOP_COOLDOWN_HOURS)).isoformat(timespec="seconds")
    if decision in (GATE_SOFT, GATE_DIRECT):
        m["count"] = m.get("count", 0) + 1
        if m["count"] >= OPEN_LOOP_MAX_MENTIONS:
            loop["status"] = "closed"
    save_profile(chat_id)
    logger.info(f"[MEMORY] Open loop {decision} for {chat_id}: {loop.get('text','')[:40]!r}")
    return would_be


def get_due_proactive_loop(chat_id: int, now_dt: Optional[datetime] = None):
    """Find the best due open loop suitable for PROACTIVE outreach (Heather texts
    first), or None.

    Unlike the reactive `_build_open_loop_prompt`, this is for the background
    initiation task and is NOT gated by INJECTION_MODE — proactive initiation has
    its own enable switch in the bot. Only DIRECT/SOFT loops qualify: SILENT means
    "color the tone, don't raise it" and NONE means not-due/closed/cooldown/low-conf.

    Returns (loop_dict, decision, reasons) or (None, GATE_NONE, reasons). The
    returned loop is a live reference into the cached profile, so the caller must
    call `commit_proactive_mention` after actually sending to update bookkeeping.
    """
    now_dt = now_dt or datetime.now()
    profile = load_profile(chat_id)
    loops = profile.get("open_loops", [])
    if not loops:
        return None, GATE_NONE, ["no_loops"]

    today = now_dt.strftime("%Y-%m-%d")
    due = [
        l for l in loops
        if isinstance(l, dict) and l.get("status") == "open"
        and str(l.get("follow_up_on", "9999")) <= today
    ]
    if not due:
        return None, GATE_NONE, ["none_due"]

    # Least-mentioned first, then oldest follow-up date.
    due.sort(key=lambda l: (l.get("mention", {}).get("count", 0), str(l.get("follow_up_on", ""))))
    for loop in due:
        decision, reasons = _gate_decision(loop, now_dt, mode="proactive")
        if decision in (GATE_SOFT, GATE_DIRECT):
            return loop, decision, reasons
    return None, GATE_NONE, ["no_eligible_due_loop"]


def commit_proactive_mention(chat_id: int, loop: dict, decision: str,
                             opener_text: str = "", now_dt: Optional[datetime] = None):
    """Record that a proactive open-loop follow-up was actually sent.

    Sets cooldown, bumps mention count, closes the loop once it has been raised
    OPEN_LOOP_MAX_MENTIONS times, logs to the shadow eval file (actually_injected
    True so it shows up in the precision review), and persists the profile."""
    now_dt = now_dt or datetime.now()
    m = loop.setdefault("mention", {"count": 0, "last_mentioned_at": None, "cooldown_until": None})
    m["last_mentioned_at"] = now_dt.isoformat(timespec="seconds")
    m["cooldown_until"] = (now_dt + timedelta(hours=OPEN_LOOP_COOLDOWN_HOURS)).isoformat(timespec="seconds")
    m["count"] = m.get("count", 0) + 1
    if m["count"] >= OPEN_LOOP_MAX_MENTIONS:
        loop["status"] = "closed"

    _shadow_log({
        "chat_id": chat_id,
        "memory_type": "open_loop_proactive",
        "memory_text": loop.get("text", ""),
        "retrieved": True,
        "confidence": loop.get("confidence"),
        "sensitivity": loop.get("sensitivity"),
        "follow_up_on": loop.get("follow_up_on"),
        "gate_decision": decision,
        "gate_reasons": ["proactive_initiation"],
        "shadow_text": opener_text.strip(),
        "actually_injected": True,
        "human_label": None,
    })
    save_profile(chat_id)
    logger.info(f"[MEMORY] Proactive open-loop {decision} sent to {chat_id}: {loop.get('text','')[:40]!r}")


def _build_emotion_pattern_hint(chat_id: int, profile: dict) -> str:
    """If recent sessions show a repeated emotional tone, surface it as a soft cue.

    Routed through INJECTION_MODE: logged in shadow, injected in live."""
    log = profile.get("emotion_log", [])
    if len(log) < 3:
        return ""
    recent = [e.get("emotion") for e in log[-4:] if isinstance(e, dict) and e.get("emotion")]
    if len(recent) < 3:
        return ""
    emotion, freq = Counter(recent).most_common(1)[0]
    if freq < 3:
        return ""

    text = (
        f"\n\n[Emotional trend — he's read as {emotion} across your recent chats. Let that "
        f"quietly inform how you treat him this turn; only name it if it feels natural, "
        f"don't diagnose him.]"
    )
    do_inject = INJECTION_MODE == "live"
    _shadow_log({
        "chat_id": chat_id,
        "memory_type": "emotion_trend",
        "memory_text": f"{emotion} x{freq}",
        "retrieved": True,
        "gate_decision": GATE_SILENT,
        "gate_reasons": ["emotion_trend"],
        "shadow_text": text.strip(),
        "actually_injected": do_inject,
        "human_label": None,
    })
    return text if do_inject else ""


def _memory_signature(profile: dict) -> str:
    """Cheap fingerprint of the episodic memory set — changes only when a
    memory is added/removed/rewritten, so we can skip re-indexing otherwise."""
    sm = profile.get("session_memories", []) or []
    mm = profile.get("memorable", []) or []
    pn = profile.get("personal_notes", []) or []
    last_date = ""
    if sm and isinstance(sm[-1], dict):
        last_date = sm[-1].get("date", "")
    return f"{len(sm)}|{len(mm)}|{len(pn)}|{last_date}"


def _build_semantic_recall(chat_id: int, current_message: str) -> str:
    """Return a compact recall hint with the episodic memories most relevant
    to the current message, or '' if disabled/unavailable/no strong match."""
    if not SEMANTIC_RECALL_ENABLED or _memory_vectors is None:
        return ""
    msg = (current_message or "").strip()
    if len(msg) < 6:
        return ""
    try:
        profile = load_profile(chat_id)
        # Index lazily, but only when the memory set actually changed.
        sig = _memory_signature(profile)
        if _vector_sync_sig.get(chat_id) != sig:
            _memory_vectors.index_profile_memories(chat_id, profile)
            _vector_sync_sig[chat_id] = sig

        hits = _memory_vectors.search(chat_id, msg, k=2, min_sim=0.6)
        if not hits:
            return ""
        snippets = []
        for text, _mtype, _sim in hits:
            t = text.strip().replace("\n", " ")
            if len(t) > 160:
                t = t[:157] + "..."
            snippets.append(f"\"{t}\"")
        joined = "; ".join(snippets)
        logger.info(f"[VECTORS] recall for {chat_id}: {len(hits)} hit(s), top sim {hits[0][2]:.2f}")
        return (
            f" [RELEVANT PAST CONTEXT (surfaced because it relates to what he just said — "
            f"weave in naturally only if it fits, don't force it): {joined}]"
        )
    except Exception as e:
        logger.debug(f"[VECTORS] recall failed for {chat_id}: {e}")
        return ""


def build_profile_prompt(chat_id: int, access_tier: str = "FREE", current_message: str = "") -> str:
    """Build a system prompt injection summarizing this user's profile.
    Returns empty string for FREE tier (except name injection if known).
    Returns empty string if profile is too thin to be useful.

    `current_message` (optional) enables semantic recall (#6): episodic
    memories most relevant to what the user just said are surfaced inline."""
    profile = load_profile(chat_id)

    # All users get full memory (no tier gate during growth phase)

    # Don't inject until we have meaningful data
    if profile["total_msgs"] < 5:
        return ""

    # If we have a consolidated memory summary, use it instead of field-by-field assembly
    memory_summary = profile.get("memory_summary")
    if memory_summary and len(memory_summary) > 50:
        name = profile.get("name")
        # Sanitize: never use Heather's own character names as the user's name.
        if isinstance(name, str) and name.strip().lower() in _HEATHER_CHARACTER_NAMES:
            name = None
        name_instruction = f"ALWAYS call him {name} (use his name at least once). " if name else ""
        history_hook = _build_history_recall(profile)
        hook_text = f" {history_hook}" if history_hook else ""

        # Relational memory — girlfriend-perspective notes (probabilistic recall)
        relational_text = _build_relational_context(profile)

        prompt = (
            f"\n\n[USER PROFILE: {memory_summary}{hook_text}"
            f"{relational_text} "
            f"Use this to personalize — {name_instruction}lean into his kinks, "
            f"remember details he shared. Don't repeat things he already knows about you. "
            f"Make him feel known and special. "
            f"NEVER address the user as Frank, Emma, Erick, Jake, or Tyler — those are Heather's own "
            f"family members, not the user. If the profile above conflates them, ignore it. "
            f"If this is a returning user, reference something specific from past sessions naturally — "
            f"like a girlfriend who remembers.]"
        )
        if _should_inject_callback(chat_id):
            prompt += _build_callback_prompt(chat_id)
            logger.info(f"[MEMORY] Injected callback prompt for {chat_id}")
        prompt += _build_open_loop_prompt(chat_id, current_message)
        prompt += _build_emotion_pattern_hint(chat_id, profile)
        prompt += _build_semantic_recall(chat_id, current_message)
        save_profile(chat_id)  # persist recall_meta updated by the recall builders
        return prompt

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

    # Relational memory — girlfriend-perspective notes (probabilistic recall)
    relational_text = _build_relational_context(profile)

    summary = " ".join(parts)

    prompt = (
        f"\n\n[USER PROFILE: {summary}{relational_text} "
        f"Use this to personalize — {name_instruction}lean into his kinks, "
        f"remember details he shared. Don't repeat things he already knows about you. "
        f"Make him feel known and special. "
        f"If this is a returning user, reference something specific from past sessions naturally — "
        f"like a girlfriend who remembers.]"
    )

    # Add callback prompt if eligible
    if _should_inject_callback(chat_id):
        prompt += _build_callback_prompt(chat_id)
        logger.info(f"[MEMORY] Injected callback prompt for {chat_id}")

    prompt += _build_open_loop_prompt(chat_id, current_message)
    prompt += _build_emotion_pattern_hint(chat_id, profile)
    prompt += _build_semantic_recall(chat_id, current_message)

    save_profile(chat_id)  # persist recall_meta updated by the recall builders
    return prompt
