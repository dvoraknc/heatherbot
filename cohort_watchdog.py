"""
Cohort Watchdog — Daily A/B tracking for HeatherBot product changes.
Compares weekly user cohorts to measure impact of:
- Proactive image on first contact (Session 1)
- Contextual voice note delay (Session 2)
- "Oh" opener fix (already deployed)

Usage:
    python cohort_watchdog.py              # Generate report
    python cohort_watchdog.py --verbose    # Include raw data
"""

import json, re, os, sys, argparse, statistics
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
BOT_DIR = Path(r"C:\Users\groot\heather-bot")
LOG_PATHS = [
    BOT_DIR / "logs" / "kelly_bot.log",
    BOT_DIR / "logs" / "heather_bot.log",
    Path(r"C:\AI\logs\kelly_bot.log"),
    Path(r"C:\AI\logs\heather_bot.log"),
]
# Include rotated logs from C:\AI\logs (the primary log location)
ROTATED_LOG_PATHS = [
    Path(r"C:\AI\logs\kelly_bot.log.1"),
    Path(r"C:\AI\logs\kelly_bot.log.2"),
    Path(r"C:\AI\logs\kelly_bot.log.3"),
    Path(r"C:\AI\logs\heather_bot.log.1"),
    Path(r"C:\AI\logs\heather_bot.log.2"),
    Path(r"C:\AI\logs\heather_bot.log.3"),
]
DISCLOSURE_FILE = BOT_DIR / "ai_disclosure_shown.json"
TIP_FILE = BOT_DIR / "tip_history.json"
REPORT_DIR = BOT_DIR / "reports"
REPORT_DIR.mkdir(exist_ok=True)

# ---------------------------------------------------------------------------
# Product change timeline (for report footer)
# ---------------------------------------------------------------------------
PRODUCT_CHANGES = [
    ("2026-03-28", '"Oh" opener prompt fix deployed'),
    ("2026-03-29", "Proactive image on first contact deployed"),
    ("2026-03-29", "Contextual voice note (delayed to msg 5-8) deployed"),
]

# ---------------------------------------------------------------------------
# Regex patterns for log parsing
# ---------------------------------------------------------------------------
# Reply line: 2026-03-20 19:34:43 | INFO     | [R31729-2079] Reply to 8391116106 (1.9s): Oh that sounds...
RE_REPLY = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}\s+\|\s+INFO\s+\|\s+"
    r"(?:\[R\d+-\d+\]\s+)?Reply to (\d+)\s+\([^)]+\):\s+(.+)"
)

# User message: 2026-03-20 19:35:39 | INFO     | [R39275-2080] Text from Josh G (8391116106) (chat): ...
RE_USER_MSG = re.compile(
    r"^(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}\s+\|\s+INFO\s+\|\s+"
    r"(?:\[R\d+-\d+\]\s+)?Text from .+?\((\d+)\)\s+\(chat\):"
)

# IMAGE_LIB sent: [IMAGE_LIB] Sent nsfw_topless_058 (nsfw_topless) to 8391116106
RE_IMAGE_LIB = re.compile(
    r"\[IMAGE_LIB\]\s+Sent\s+\S+\s+\(\S+\)\s+to\s+(\d+)"
)

# Proactive selfie / library photo: Proactive selfie for 8391116106 / Proactive library photo sent to 8391116106
RE_PROACTIVE_IMG = re.compile(
    r"Proactive (?:selfie|library photo)\s+(?:for|sent to)\s+(\d+)"
)

# Voice note: Sent voice note to @Cfloyd806 (1416151214)
RE_VOICE_NOTE = re.compile(
    r"Sent voice note to .+?\((\d+)\)"
)

# Voice welcome: [WELCOME] Sent voice welcome to @TON3RD (1704189714)
RE_VOICE_WELCOME = re.compile(
    r"\[WELCOME\]\s+Sent voice welcome to .+?\((\d+)\)"
)

# Video sent: Video sent to 1416151214: vid_061.mp4
RE_VIDEO_SENT = re.compile(
    r"Video sent to (\d+):"
)

# Sending cached video: Sending cached video vid_061.mp4 to 1416151214
RE_VIDEO_CACHED = re.compile(
    r"Sending cached video .+ to (\d+)"
)


# ---------------------------------------------------------------------------
# Helper: ISO week label  (e.g. "W11 (Mar 10)")
# ---------------------------------------------------------------------------
def week_label(dt):
    iso = dt.isocalendar()
    # Monday of that ISO week
    monday = datetime.strptime(f"{iso[0]} {iso[1]} 1", "%G %V %u")
    return f"W{iso[1]:02d} ({monday.strftime('%b %d')})"


def iso_week_key(dt):
    """Return (iso_year, iso_week) tuple for grouping."""
    iso = dt.isocalendar()
    return (iso[0], iso[1])


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------
def load_disclosure():
    """Load ai_disclosure_shown.json -> {user_id: {timestamp, source, username}}."""
    if not DISCLOSURE_FILE.exists():
        print(f"[WARN] Disclosure file not found: {DISCLOSURE_FILE}")
        return {}
    try:
        with open(DISCLOSURE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        print(f"[WARN] Failed to load disclosure file: {e}")
        return {}


def load_tip_history():
    """Load tip_history.json -> {user_id: {...}}."""
    if not TIP_FILE.exists():
        print(f"[WARN] Tip history file not found: {TIP_FILE}")
        return {}
    try:
        with open(TIP_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data
    except Exception as e:
        print(f"[WARN] Failed to load tip history: {e}")
        return {}


def iter_log_lines():
    """Yield deduplicated lines from all log files (primary + rotated + fallback).

    Uses a set of (date_prefix, truncated_line) to avoid counting duplicates
    when the same events appear in both log locations.
    """
    seen = set()
    # Process rotated logs first (oldest to newest), then current logs
    all_paths = list(reversed(ROTATED_LOG_PATHS)) + list(LOG_PATHS)
    for log_path in all_paths:
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.rstrip("\n\r")
                    if not line:
                        continue
                    # Dedup key: first 120 chars is enough to identify unique events
                    dedup_key = line[:120]
                    if dedup_key not in seen:
                        seen.add(dedup_key)
                        yield line
        except Exception as e:
            print(f"[WARN] Error reading {log_path}: {e}")


# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------
def parse_logs():
    """Parse all log files and return structured data.

    Returns:
        user_msg_counts: {user_id: total_message_count}
        user_msg_dates:  {user_id: [date_str, ...]}  (dates of messages)
        reply_data:      [(date_str, user_id, response_text), ...]
        image_events:    {user_id: count}
        proactive_events:{user_id: count}
        voice_events:    {user_id: count}
        voice_welcome:   {user_id: True}
        video_events:    {user_id: count}
        user_first_msgs: {user_id: [ordered list of event types for first N events]}
    """
    user_msg_counts = Counter()
    user_msg_dates = defaultdict(list)
    reply_data = []
    image_events = Counter()
    proactive_events = Counter()
    voice_events = Counter()
    voice_welcome_users = set()
    video_events = Counter()

    # Track per-user event sequence for "early image" detection
    # Each entry: (event_order_within_user, event_type)
    user_event_seq = defaultdict(list)  # user_id -> [(global_order, type)]
    global_order = 0

    for line in iter_log_lines():
        # User messages
        m = RE_USER_MSG.match(line)
        if m:
            date_str, uid = m.group(1), m.group(2)
            user_msg_counts[uid] += 1
            user_msg_dates[uid].append(date_str)
            global_order += 1
            user_event_seq[uid].append((global_order, "msg"))
            continue

        # Bot replies
        m = RE_REPLY.match(line)
        if m:
            date_str, uid, text = m.group(1), m.group(2), m.group(3)
            reply_data.append((date_str, uid, text))
            global_order += 1
            user_event_seq[uid].append((global_order, "reply"))
            continue

        # IMAGE_LIB events
        m = RE_IMAGE_LIB.search(line)
        if m:
            uid = m.group(1)
            image_events[uid] += 1
            global_order += 1
            user_event_seq[uid].append((global_order, "image"))
            continue

        # Proactive image events
        m = RE_PROACTIVE_IMG.search(line)
        if m:
            uid = m.group(1)
            proactive_events[uid] += 1
            global_order += 1
            user_event_seq[uid].append((global_order, "proactive_img"))
            continue

        # Voice welcome
        m = RE_VOICE_WELCOME.search(line)
        if m:
            uid = m.group(1)
            voice_welcome_users.add(uid)
            global_order += 1
            user_event_seq[uid].append((global_order, "voice_welcome"))
            continue

        # Voice notes (general)
        m = RE_VOICE_NOTE.search(line)
        if m:
            uid = m.group(1)
            voice_events[uid] += 1
            global_order += 1
            user_event_seq[uid].append((global_order, "voice"))
            continue

        # Video sent
        m = RE_VIDEO_SENT.search(line)
        if m:
            uid = m.group(1)
            video_events[uid] += 1
            global_order += 1
            user_event_seq[uid].append((global_order, "video"))
            continue

        # Video cached send
        m = RE_VIDEO_CACHED.search(line)
        if m:
            uid = m.group(1)
            # Don't double-count with VIDEO_SENT (the cached line comes first)
            # We'll just track as event sequence but not increment counter
            global_order += 1
            user_event_seq[uid].append((global_order, "video_cache"))

    # Determine who got an early image (IMAGE_LIB in first 5 user-facing events)
    early_image_users = set()
    for uid, events in user_event_seq.items():
        # Sort by global order, take first 5 events that are user-facing
        events.sort(key=lambda x: x[0])
        user_facing = [e for e in events if e[1] in ("msg", "reply", "image", "proactive_img", "voice_welcome", "voice")]
        first_five = user_facing[:5]
        if any(e[1] in ("image", "proactive_img") for e in first_five):
            early_image_users.add(uid)

    # Determine who got contextual voice (voice note but NOT as welcome, delivered after first few messages)
    contextual_voice_users = set()
    for uid, events in user_event_seq.items():
        events.sort(key=lambda x: x[0])
        msg_count = 0
        for _, etype in events:
            if etype == "msg":
                msg_count += 1
            elif etype == "voice" and msg_count >= 3:
                contextual_voice_users.add(uid)
                break

    return {
        "user_msg_counts": user_msg_counts,
        "user_msg_dates": user_msg_dates,
        "reply_data": reply_data,
        "image_events": image_events,
        "proactive_events": proactive_events,
        "voice_events": voice_events,
        "voice_welcome_users": voice_welcome_users,
        "video_events": video_events,
        "early_image_users": early_image_users,
        "contextual_voice_users": contextual_voice_users,
    }


# ---------------------------------------------------------------------------
# Cohort building
# ---------------------------------------------------------------------------
def build_cohorts(disclosure, tip_history, log_data):
    """Group users into weekly cohorts by signup date.

    Returns: {(iso_year, iso_week): {metrics dict}}
    """
    cohorts = defaultdict(lambda: {
        "signup_count": 0,
        "activated": 0,        # 1+ messages in logs
        "engaged": 0,          # 10+ messages
        "power": 0,            # 50+ messages
        "bounced": 0,          # signed up but <10 messages
        "tipped": 0,           # any tip
        "total_stars": 0,
        "got_early_image": 0,
        "got_voice_welcome": 0,
        "got_contextual_voice": 0,
        "got_proactive_image": 0,
        "message_counts": [],  # for median calculation
        "user_ids": [],
    })

    user_msg_counts = log_data["user_msg_counts"]
    early_image_users = log_data["early_image_users"]
    voice_welcome_users = log_data["voice_welcome_users"]
    contextual_voice_users = log_data["contextual_voice_users"]
    proactive_events = log_data["proactive_events"]

    for uid, info in disclosure.items():
        ts_str = info.get("timestamp")
        if not ts_str:
            continue  # Skip users without signup timestamp

        try:
            signup_dt = datetime.fromisoformat(ts_str)
        except (ValueError, TypeError):
            continue

        wk = iso_week_key(signup_dt)
        c = cohorts[wk]
        c["signup_count"] += 1
        c["user_ids"].append(uid)

        msg_count = user_msg_counts.get(uid, 0)
        c["message_counts"].append(msg_count)

        if msg_count >= 1:
            c["activated"] += 1
        if msg_count >= 10:
            c["engaged"] += 1
        if msg_count >= 50:
            c["power"] += 1
        if msg_count < 10:
            c["bounced"] += 1

        # Tip data
        tip_info = tip_history.get(uid, {})
        if not isinstance(tip_info, dict):
            tip_info = {}
        stars = tip_info.get("total_stars", 0)
        if stars > 0:
            c["tipped"] += 1
            c["total_stars"] += stars

        # Feature flags
        if uid in early_image_users:
            c["got_early_image"] += 1
        if uid in voice_welcome_users:
            c["got_voice_welcome"] += 1
        if uid in contextual_voice_users:
            c["got_contextual_voice"] += 1
        if proactive_events.get(uid, 0) > 0:
            c["got_proactive_image"] += 1

    return cohorts


# ---------------------------------------------------------------------------
# "Oh" opener tracking by response week
# ---------------------------------------------------------------------------
def build_oh_tracking(reply_data):
    """Track "Oh" opener rate per week of the RESPONSE (not signup week).

    Returns: {(iso_year, iso_week): {"total": N, "oh_count": N}}
    """
    oh_weeks = defaultdict(lambda: {"total": 0, "oh_count": 0})

    for date_str, uid, text in reply_data:
        try:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            continue

        wk = iso_week_key(dt)
        oh_weeks[wk]["total"] += 1

        # Check if response starts with "Oh" (case-sensitive, with optional comma/space)
        stripped = text.lstrip()
        if re.match(r"^Oh[,\s!]", stripped) or stripped == "Oh":
            oh_weeks[wk]["oh_count"] += 1

    return oh_weeks


# ---------------------------------------------------------------------------
# Rate helpers
# ---------------------------------------------------------------------------
def pct(num, denom):
    if denom == 0:
        return 0.0
    return round(100.0 * num / denom, 1)


def median_val(lst):
    if not lst:
        return 0
    return int(statistics.median(lst))


def fmt_pct(val):
    return f"{val:.1f}%"


def fmt_change(baseline, current):
    """Format change as +X.X pp or -X.X pp with impact arrow."""
    diff = current - baseline
    if abs(diff) < 0.5:
        return f"{diff:+.1f} pp", "--"
    elif diff > 0:
        return f"{diff:+.1f} pp", "UP"
    else:
        return f"{diff:+.1f} pp", "DOWN"


# ---------------------------------------------------------------------------
# Report generation
# ---------------------------------------------------------------------------
def generate_report(cohorts, oh_tracking, verbose=False):
    today = datetime.now().strftime("%Y-%m-%d")
    lines = []

    lines.append(f"# Cohort Watchdog Report -- {today}")
    lines.append("")

    # Sort weeks chronologically
    sorted_weeks = sorted(cohorts.keys())
    if not sorted_weeks:
        lines.append("**No cohort data available.** No users with signup timestamps found.")
        report_text = "\n".join(lines)
        return report_text

    # ---- Weekly Cohort Comparison table ----
    lines.append("## Weekly Cohort Comparison")
    lines.append("")
    lines.append("| Week | Signups | Activated | Engaged | Power | Bounced | Tipped | Stars | Early Img | Voice Welc | Ctx Voice | Proactive Img | Median Msgs | Oh% |")
    lines.append("|------|---------|-----------|---------|-------|---------|--------|-------|-----------|------------|-----------|---------------|-------------|-----|")

    for wk in sorted_weeks:
        c = cohorts[wk]
        dt_monday = datetime.strptime(f"{wk[0]} {wk[1]} 1", "%G %V %u")
        label = f"W{wk[1]:02d} ({dt_monday.strftime('%b %d')})"
        s = c["signup_count"]
        act_r = pct(c["activated"], s)
        eng_r = pct(c["engaged"], s)
        pow_r = pct(c["power"], s)
        bnc_r = pct(c["bounced"], s)
        tip_r = pct(c["tipped"], s)
        ei = pct(c["got_early_image"], s)
        vw = pct(c["got_voice_welcome"], s)
        cv = pct(c["got_contextual_voice"], s)
        pi = pct(c["got_proactive_image"], s)
        med = median_val(c["message_counts"])

        # Oh% for this cohort's response week
        oh_info = oh_tracking.get(wk, {"total": 0, "oh_count": 0})
        oh_rate = pct(oh_info["oh_count"], oh_info["total"])

        lines.append(
            f"| {label} | {s} | {c['activated']} ({act_r}%) | {c['engaged']} ({eng_r}%) | "
            f"{c['power']} ({pow_r}%) | {c['bounced']} ({bnc_r}%) | "
            f"{c['tipped']} ({tip_r}%) | {c['total_stars']} | "
            f"{ei}% | {vw}% | {cv}% | {pi}% | {med} | {oh_rate}% |"
        )

    lines.append("")

    # ---- Baseline vs Recent ----
    lines.append("## Baseline vs Recent")
    lines.append("")

    # Dynamic baseline: 2 oldest complete weeks
    # A "complete" week is one that ended before today
    today_dt = datetime.now()
    today_wk = iso_week_key(today_dt)
    complete_weeks = [wk for wk in sorted_weeks if wk < today_wk]

    if len(complete_weeks) >= 3:
        baseline_weeks = complete_weeks[:2]
        latest_week = complete_weeks[-1]

        # Aggregate baseline
        bl = {"signup": 0, "activated": 0, "engaged": 0, "bounced": 0, "tipped": 0, "stars": 0, "msgs": []}
        for bwk in baseline_weeks:
            bc = cohorts[bwk]
            bl["signup"] += bc["signup_count"]
            bl["activated"] += bc["activated"]
            bl["engaged"] += bc["engaged"]
            bl["bounced"] += bc["bounced"]
            bl["tipped"] += bc["tipped"]
            bl["stars"] += bc["total_stars"]
            bl["msgs"].extend(bc["message_counts"])

        lc = cohorts[latest_week]

        bl_act_r = pct(bl["activated"], bl["signup"])
        bl_eng_r = pct(bl["engaged"], bl["signup"])
        bl_bnc_r = pct(bl["bounced"], bl["signup"])
        bl_tip_r = pct(bl["tipped"], bl["signup"])
        bl_med = median_val(bl["msgs"])

        lt_act_r = pct(lc["activated"], lc["signup_count"])
        lt_eng_r = pct(lc["engaged"], lc["signup_count"])
        lt_bnc_r = pct(lc["bounced"], lc["signup_count"])
        lt_tip_r = pct(lc["tipped"], lc["signup_count"])
        lt_med = median_val(lc["message_counts"])

        bl_wk_labels = [f"W{w[1]:02d}" for w in baseline_weeks]
        bl_label = "-".join(bl_wk_labels)
        lt_monday = datetime.strptime(f"{latest_week[0]} {latest_week[1]} 1", "%G %V %u")
        lt_label = f"W{latest_week[1]:02d} ({lt_monday.strftime('%b %d')})"

        lines.append(f"| Metric | Baseline ({bl_label}) | Latest ({lt_label}) | Change | Impact |")
        lines.append("|--------|" + "-" * 22 + "|" + "-" * 22 + "|--------|--------|")

        metrics = [
            ("Activation Rate", bl_act_r, lt_act_r),
            ("Engagement Rate", bl_eng_r, lt_eng_r),
            ("Bounce Rate", bl_bnc_r, lt_bnc_r),
            ("Tip Rate", bl_tip_r, lt_tip_r),
            ("Median Messages", float(bl_med), float(lt_med)),
        ]

        for name, bv, lv in metrics:
            if name == "Median Messages":
                change_str = f"{lv - bv:+.0f}"
                impact = "UP" if lv > bv else ("DOWN" if lv < bv else "--")
                lines.append(f"| {name} | {bv:.0f} | {lv:.0f} | {change_str} | {impact} |")
            else:
                ch, imp = fmt_change(bv, lv)
                lines.append(f"| {name} | {fmt_pct(bv)} | {fmt_pct(lv)} | {ch} | {imp} |")
    else:
        lines.append("*Not enough complete weeks for baseline comparison (need 3+).*")

    lines.append("")

    # ---- "Oh" Opener Tracking ----
    lines.append('## "Oh" Opener Tracking')
    lines.append("")
    lines.append("Tracks % of bot responses starting with \"Oh\" per response week.")
    lines.append("")
    lines.append("| Week | Total Responses | Starting with \"Oh\" | Rate | Trend |")
    lines.append("|------|----------------|-------------------|------|-------|")

    sorted_oh_weeks = sorted(oh_tracking.keys())
    prev_rate = None
    for wk in sorted_oh_weeks:
        info = oh_tracking[wk]
        if info["total"] == 0:
            continue
        dt_monday = datetime.strptime(f"{wk[0]} {wk[1]} 1", "%G %V %u")
        label = f"W{wk[1]:02d} ({dt_monday.strftime('%b %d')})"
        rate = pct(info["oh_count"], info["total"])
        if prev_rate is not None:
            diff = rate - prev_rate
            if abs(diff) < 0.3:
                trend = "~"
            elif diff > 0:
                trend = f"+{diff:.1f} pp"
            else:
                trend = f"{diff:.1f} pp"
        else:
            trend = "baseline"
        lines.append(f"| {label} | {info['total']} | {info['oh_count']} | {rate}% | {trend} |")
        prev_rate = rate

    lines.append("")

    # ---- Alerts ----
    lines.append("## Alerts")
    lines.append("")
    alerts = []

    if len(complete_weeks) >= 3:
        # Check for significant changes
        if lt_bnc_r - bl_bnc_r > 5:
            alerts.append(f"Bounce rate increased {lt_bnc_r - bl_bnc_r:+.1f} pp vs baseline (now {lt_bnc_r}%)")
        if lt_act_r - bl_act_r < -5:
            alerts.append(f"Activation rate dropped {lt_act_r - bl_act_r:+.1f} pp vs baseline (now {lt_act_r}%)")
        if lt_eng_r - bl_eng_r > 5:
            alerts.append(f"Engagement rate improved {lt_eng_r - bl_eng_r:+.1f} pp vs baseline (now {lt_eng_r}%)")
        if lt_eng_r - bl_eng_r < -5:
            alerts.append(f"Engagement rate dropped {lt_eng_r - bl_eng_r:+.1f} pp vs baseline (now {lt_eng_r}%)")
        if lt_tip_r - bl_tip_r > 3:
            alerts.append(f"Tip rate improved {lt_tip_r - bl_tip_r:+.1f} pp vs baseline (now {lt_tip_r}%)")

    # Check Oh% trend
    if len(sorted_oh_weeks) >= 2:
        first_oh = oh_tracking[sorted_oh_weeks[0]]
        last_oh = oh_tracking[sorted_oh_weeks[-1]]
        if first_oh["total"] > 0 and last_oh["total"] > 0:
            first_rate = pct(first_oh["oh_count"], first_oh["total"])
            last_rate = pct(last_oh["oh_count"], last_oh["total"])
            if last_rate < first_rate - 3:
                alerts.append(f'"Oh" opener rate decreased from {first_rate}% to {last_rate}% (fix working)')
            elif last_rate > first_rate + 3:
                alerts.append(f'"Oh" opener rate increased from {first_rate}% to {last_rate}% (regression?)')

    if not alerts:
        alerts.append("No significant changes detected.")

    for alert in alerts:
        lines.append(f"- {alert}")

    lines.append("")

    # ---- Product Change Timeline ----
    lines.append("## Product Change Timeline")
    lines.append("")
    for date, desc in PRODUCT_CHANGES:
        lines.append(f"- {date}: {desc}")

    lines.append("")

    # ---- Verbose: raw per-user data ----
    if verbose:
        lines.append("## Raw Data (--verbose)")
        lines.append("")
        lines.append("| User ID | Signup | Source | Messages | Tipped | Stars | Early Img | Voice |")
        lines.append("|---------|--------|--------|----------|--------|-------|-----------|-------|")
        # flatten all cohort users
        for wk in sorted_weeks:
            c = cohorts[wk]
            for uid in c["user_ids"]:
                # Reconstruct per-user info
                disc = disclosure_global.get(uid, {})
                tip = tip_history_global.get(uid, {})
                ts = disc.get("timestamp", "?")[:10] if disc.get("timestamp") else "?"
                src = disc.get("source", "?")
                msgs = log_data_global["user_msg_counts"].get(uid, 0)
                stars = tip.get("total_stars", 0)
                tipped_yn = "Y" if stars > 0 else ""
                ei = "Y" if uid in log_data_global["early_image_users"] else ""
                vw = "Y" if uid in log_data_global["voice_welcome_users"] else ""
                lines.append(f"| {uid} | {ts} | {src} | {msgs} | {tipped_yn} | {stars} | {ei} | {vw} |")
        lines.append("")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
# Globals for verbose mode cross-referencing
disclosure_global = {}
tip_history_global = {}
log_data_global = {}


def main():
    global disclosure_global, tip_history_global, log_data_global

    parser = argparse.ArgumentParser(description="Cohort Watchdog — Daily A/B tracking")
    parser.add_argument("--verbose", action="store_true", help="Include raw per-user data")
    args = parser.parse_args()

    print("Cohort Watchdog starting...")
    print()

    # Load data sources
    print("[1/4] Loading disclosure data...")
    disclosure = load_disclosure()
    disclosure_global = disclosure
    users_with_ts = sum(1 for v in disclosure.values() if v.get("timestamp"))
    print(f"  {len(disclosure)} total users, {users_with_ts} with signup timestamps")

    print("[2/4] Loading tip history...")
    tip_history = load_tip_history()
    tip_history_global = tip_history
    tippers = sum(1 for v in tip_history.values() if isinstance(v, dict) and v.get("total_stars", 0) > 0)
    total_stars = sum(v.get("total_stars", 0) for v in tip_history.values() if isinstance(v, dict))
    print(f"  {len(tip_history)} users tracked, {tippers} tippers, {total_stars} total stars")

    print("[3/4] Parsing bot logs (this may take a moment)...")
    log_data = parse_logs()
    log_data_global = log_data
    print(f"  {len(log_data['user_msg_counts'])} users with messages")
    print(f"  {sum(log_data['user_msg_counts'].values())} total user messages")
    print(f"  {len(log_data['reply_data'])} bot replies parsed")
    print(f"  {sum(log_data['image_events'].values())} image deliveries")
    print(f"  {sum(log_data['voice_events'].values())} voice notes")
    print(f"  {sum(log_data['video_events'].values())} video sends")
    print(f"  {len(log_data['voice_welcome_users'])} voice welcomes")
    print(f"  {len(log_data['early_image_users'])} users got early image")
    print(f"  {len(log_data['contextual_voice_users'])} users got contextual voice")

    print("[4/4] Building cohorts and generating report...")
    cohorts = build_cohorts(disclosure, tip_history, log_data)
    oh_tracking = build_oh_tracking(log_data["reply_data"])

    report = generate_report(cohorts, oh_tracking, verbose=args.verbose)

    # Save report
    today = datetime.now().strftime("%Y-%m-%d")
    report_path = REPORT_DIR / f"cohort_watchdog_{today}.md"
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(report)

    print()
    print(f"Report saved to: {report_path}")
    print()

    # Print summary to stdout
    sorted_weeks = sorted(cohorts.keys())
    if sorted_weeks:
        print("=" * 70)
        print("COHORT SUMMARY")
        print("=" * 70)
        for wk in sorted_weeks:
            c = cohorts[wk]
            dt_monday = datetime.strptime(f"{wk[0]} {wk[1]} 1", "%G %V %u")
            label = f"W{wk[1]:02d} ({dt_monday.strftime('%b %d')})"
            s = c["signup_count"]
            act_r = pct(c["activated"], s)
            eng_r = pct(c["engaged"], s)
            bnc_r = pct(c["bounced"], s)
            med = median_val(c["message_counts"])
            print(f"  {label}: {s:3d} signups | {act_r:5.1f}% activated | {eng_r:5.1f}% engaged | {bnc_r:5.1f}% bounced | median {med} msgs")

        # Oh% summary
        print()
        print("Oh% by response week:")
        sorted_oh = sorted(oh_tracking.keys())
        for wk in sorted_oh:
            info = oh_tracking[wk]
            if info["total"] == 0:
                continue
            dt_monday = datetime.strptime(f"{wk[0]} {wk[1]} 1", "%G %V %u")
            label = f"W{wk[1]:02d} ({dt_monday.strftime('%b %d')})"
            rate = pct(info["oh_count"], info["total"])
            print(f"  {label}: {info['oh_count']:4d}/{info['total']:4d} = {rate:5.1f}%")

    print()
    print("Done.")


if __name__ == "__main__":
    main()
