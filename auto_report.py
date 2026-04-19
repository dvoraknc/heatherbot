#!/usr/bin/env python3
"""
HeatherBot Auto Report — runs every 12 hours via Task Scheduler.
Generates operational report, detects issues, applies safe auto-fixes,
and sends the report to admin via Telegram Bot API.

Usage:
    python auto_report.py              # 12-hour report (default)
    python auto_report.py 24           # 24-hour report
    python auto_report.py --dry-run    # Generate report but don't send or fix
    python auto_report.py --notify-changes  # Check for pending code changes and notify admin

Changes log: C:\AI\logs\auto_changes.log
Reports log: C:\AI\logs\reports\
"""

import io, json, os, re, sys, time, urllib.request, urllib.parse
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# -- Config ----------------------------------------------------------------
BOT_DIR = Path(r"C:\Users\groot\heather-bot")
LOG_FILE_PRIMARY = Path(r"C:\AI\logs\heather_bot.log")
LOG_FILE_FALLBACK = BOT_DIR / "logs" / "heather_bot.log"
LOG_FILE_PRIMARY_KELLY = Path(r"C:\AI\logs\kelly_bot.log")
LOG_FILE_FALLBACK_KELLY = BOT_DIR / "logs" / "kelly_bot.log"
REPORTS_DIR = Path(r"C:\AI\logs\reports")
CHANGES_LOG = Path(r"C:\AI\logs\auto_changes.log")
CHANGES_NOTIFIED = Path(r"C:\AI\logs\auto_changes_notified.pos")  # tracks last notified line
BOT_SCRIPT = BOT_DIR / "kelly_telegram_bot.py"

# Load .env for bot token and admin ID
_env = {}
_env_path = BOT_DIR / ".env"
if _env_path.exists():
    for line in _env_path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            _env[k.strip()] = v.strip()

BOT_TOKEN = _env.get("PAYMENT_BOT_TOKEN", _env.get("TELEGRAM_BOT_TOKEN", ""))
ADMIN_ID = _env.get("ADMIN_USER_ID", "")

# -- Thresholds for auto-fix -----------------------------------------------
TRUNCATION_RATE_THRESHOLD = 0.05   # 5% of replies = too many truncations
CSAM_FLAG_THRESHOLD = 8            # Flag users with 8+ hits for admin review
LATENCY_SPIKE_THRESHOLD = 20.0     # Seconds -- flag if max latency exceeds this
ERROR_THRESHOLD = 5                # Flag if errors exceed this count

# -- Helpers ----------------------------------------------------------------
def log_change(msg: str):
    """Append to auto_changes.log with timestamp."""
    CHANGES_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(CHANGES_LOG, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {msg}\n")


def send_telegram(text: str, chat_id: str = None):
    """Send a message to admin via Telegram Bot API (bypasses Telethon session lock)."""
    if not BOT_TOKEN or not (chat_id or ADMIN_ID):
        print("WARNING: No BOT_TOKEN or ADMIN_ID -- cannot send Telegram message")
        return False

    target = chat_id or ADMIN_ID
    # Telegram max message length is 4096 chars
    chunks = [text[i:i+4000] for i in range(0, len(text), 4000)]

    for chunk in chunks:
        data = urllib.parse.urlencode({
            "chat_id": target,
            "text": chunk,
            "parse_mode": "HTML",
        }).encode("utf-8")
        try:
            req = urllib.request.Request(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            urllib.request.urlopen(req, timeout=15)
        except Exception as e:
            print(f"WARNING: Telegram send failed: {e}")
            return False
    return True


# -- Log Parsing ------------------------------------------------------------
def _read_log_lines(cutoff_str: str) -> list:
    """Read log lines from both primary and fallback log paths.
    Returns merged, deduplicated lines newer than cutoff_str."""
    lines = []
    for log_path in [LOG_FILE_PRIMARY_KELLY, LOG_FILE_FALLBACK_KELLY, LOG_FILE_PRIMARY, LOG_FILE_FALLBACK]:
        if not log_path.exists():
            continue
        try:
            with open(log_path, "r", encoding="utf-8", errors="ignore") as f:
                lines.extend(l for l in f if l[:16] >= cutoff_str)
        except (OSError, IOError):
            continue
    # Deduplicate (same bot won't log to both, but be safe)
    seen = set()
    unique = []
    for l in lines:
        if l not in seen:
            seen.add(l)
            unique.append(l)
    return unique


def parse_log(hours: int) -> dict:
    """Parse bot logs for the given time window and return metrics."""
    cutoff = datetime.now() - timedelta(hours=hours)
    cutoff_str = cutoff.strftime("%Y-%m-%d %H:%M")

    lines = _read_log_lines(cutoff_str)

    # -- Volume --
    users = Counter()
    user_ids = {}  # display_name -> chat_id
    for l in lines:
        m = re.search(r"Text from (.+?) \((\d+)\)", l)
        if m:
            key = f"{m.group(1)} ({m.group(2)})"
            users[key] += 1
            user_ids[key] = m.group(2)

    total_msgs = sum(users.values())

    # -- Latency --
    latencies = []
    for l in lines:
        m = re.search(r"Reply to \d+ \(([0-9.]+)s\)", l)
        if m:
            latencies.append(float(m.group(1)))

    # -- Image gen --
    img_gen = sum(1 for l in lines if "NSFW image" in l and "NSFW Master" in l)
    img_anatomy = sum(1 for l in lines if "anatomy LoRA" in l)
    img_lib = sum(1 for l in lines if "Served library image" in l)
    img_cap = sum(1 for l in lines if "Photo cap reached" in l)
    img_fail = sum(1 for l in lines if "image generation failed" in l.lower()
                   or ("ComfyUI" in l and "error" in l.lower()))

    # -- Videos --
    vids = sum(1 for l in lines if "Video sent to" in l or "Sent cached video" in l)
    vid_accepts = sum(1 for l in lines if "offer acceptance" in l)

    # -- Stories --
    stories = sum(1 for l in lines if "story" in l.lower()
                  and ("served" in l.lower() or "banked" in l.lower()))

    # -- Errors & truncations --
    errors = [l.strip() for l in lines if "| ERROR" in l]
    truncations = sum(1 for l in lines if "Truncated" in l and "WARNING" in l)

    # -- Dissatisfaction --
    dissatisfaction = [l.strip() for l in lines if "DISSATISFACTION" in l]

    # -- CSAM --
    csam = [l.strip() for l in lines if "CSAM-FLAG" in l]
    csam_users = Counter()
    for c in csam:
        m = re.search(r"from (.+?) \((\d+)\)", c)
        if m:
            csam_users[f"{m.group(1)} ({m.group(2)})"] += 1

    # -- Steering --
    steered = sum(1 for l in lines if "STEERING" in l or "Suppressed" in l)

    # -- Disclosures (new users) --
    disclosures = sum(1 for l in lines if "disclosure" in l.lower()
                      and ("shown" in l.lower() or "sent" in l.lower()))

    # -- Gender violations --
    gender = sum(1 for l in lines if "Gender violation" in l)
    incomplete = sum(1 for l in lines if "Incomplete response" in l)

    # -- Positive / negative sentiment --
    pos_words = [
        "love it", "so hot", "damn", "wow", "fuck yes", "amazing", "beautiful",
        "gorgeous", "sexy", "perfect", "incredible", "holy shit", "omg", "more please",
        "send more", "cum", "came", "stroking", "jerking", "hard for you",
        "making me hard", "so wet", "turned on", "horny", "love that", "thats hot",
        "mmm", "yesss", "fuuuck", "love you", "youre the best", "good girl",
    ]
    pos_count = 0
    for l in lines:
        m = re.search(r"Text from .+? \(\d+\).*?: (.+)", l)
        if m and any(pw in m.group(1).lower() for pw in pos_words):
            pos_count += 1

    neg_words = ["bot", "fake", "scam", "waste", "leaving", "block",
                 "report", "not real", "ai generated", "goodbye"]
    neg_count = 0
    neg_examples = []
    for l in lines:
        m = re.search(r"Text from (.+?) \(\d+\).*?: (.+)", l)
        if m:
            msg = m.group(2).lower()
            if any(nw in msg for nw in neg_words):
                neg_count += 1
                if len(neg_examples) < 5:
                    neg_examples.append(f"  {m.group(1)}: {m.group(2)[:80]}")

    # -- Goodbyes --
    bye_users = set()
    for l in lines:
        m = re.search(r"Text from (.+?) \(\d+\).*?: (.+)", l)
        if m:
            msg = m.group(2).lower()
            if any(w in msg for w in ["bye", "goodbye", "good night",
                                       "gotta go", "ttyl", "talk later"]):
                bye_users.add(m.group(1))

    # -- Slow replies --
    slow = sum(
        1 for l in lines
        if re.search(r"Reply to \d+ \(([0-9.]+)s\)", l)
        and float(re.search(r"\(([0-9.]+)s\)", l).group(1)) > 5.0
    )

    # -- Engaged vs casual --
    engaged = {u: c for u, c in users.items() if c >= 10}
    casual = {u: c for u, c in users.items() if c < 10}

    # -- Breeding / CNC / Frank --
    breeding_words = [
        "breed", "breeding", "knock", "pregnant", "impregnate", "seed",
        "fertility", "sperm", "womb", "cum inside", "fill me", "put a baby",
        "knock me up", "knocked up", "make me pregnant", "cnc",
        "overpower", "pin me down", "frank", "carry your", "swell",
        "breed me", "bred", "breeding bitch",
    ]
    breeding_user = []
    breeding_bot = []
    frank_bot = []
    for l in lines:
        m = re.search(r"Text from (.+?) \((\d+)\).*?: (.+)", l)
        if m:
            msg = m.group(3).lower()
            if any(bw in msg for bw in breeding_words):
                breeding_user.append(f"  {m.group(1)}: {m.group(3)[:100]}")

        m2 = re.search(r"Reply to (\d+) \([0-9.]+s\): (.+)", l)
        if m2:
            reply = m2.group(2).lower()
            if any(bw in reply for bw in breeding_words):
                breeding_bot.append(f"  to {m2.group(1)}: {m2.group(2)[:100]}")
            if "frank" in reply:
                frank_bot.append(f"  to {m2.group(1)}: {m2.group(2)[:100]}")

    # -- Breeding injection count --
    breeding_injections = sum(1 for l in lines if "[BREEDING] Injected" in l)

    # -- New user sources (from disclosure logs) --
    source_counts = Counter()
    for l in lines:
        m = re.search(r"\[DISCLOSURE\] New user.*?source: (\w+)", l)
        if m:
            source_counts[m.group(1)] += 1

    # =====================================================================
    # NEW METRICS
    # =====================================================================

    # -- 1. Retention: returning users (seen in a previous 24hr window) --
    # Check disclosure file for users who were disclosed BEFORE this window
    returning_users = 0
    disclosure_file = BOT_DIR / "ai_disclosure_shown.json"
    old_user_ids = set()
    if disclosure_file.exists():
        try:
            data = json.loads(disclosure_file.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                for uid_str, info in data.items():
                    ts_str = info.get("timestamp") if isinstance(info, dict) else None
                    if ts_str and ts_str < cutoff_str:
                        old_user_ids.add(uid_str)
            elif isinstance(data, list):
                old_user_ids = {str(x) for x in data}
        except Exception:
            pass

    for u_key, uid in user_ids.items():
        if uid in old_user_ids:
            returning_users += 1
    new_users_chatting = len(user_ids) - returning_users

    # -- 2. Conversation depth: avg msgs per engaged user --
    avg_depth = (sum(engaged.values()) / len(engaged)) if engaged else 0

    # -- 3. Revenue signals: tips, payment bot starts --
    tip_signals = sum(1 for l in lines if "tip" in l.lower()
                      and ("signal" in l.lower() or "detected" in l.lower()
                           or "payment" in l.lower() or "TIP" in l))
    payment_starts = sum(1 for l in lines if "started payment" in l.lower()
                         or "payment bot" in l.lower())

    # -- 4. Voice note usage --
    voice_requests = sum(1 for l in lines if "voice" in l.lower()
                         and ("request" in l.lower() or "sending" in l.lower()
                              or "sent" in l.lower()))
    voice_failures = sum(1 for l in lines if "voice" in l.lower()
                         and ("fail" in l.lower() or "error" in l.lower()))

    # -- 5. Time-of-day heatmap (4hr buckets) --
    hour_buckets = Counter()
    for l in lines:
        m = re.search(r"Text from .+? \(\d+\)", l)
        if m:
            try:
                hr = int(l[11:13])
                bucket = f"{(hr // 4) * 4:02d}-{(hr // 4) * 4 + 3:02d}"
                hour_buckets[bucket] += 1
            except (ValueError, IndexError):
                pass

    # -- 6. Photo-to-chat ratio --
    total_photos = img_gen + img_lib
    photo_chat_ratio = total_photos / total_msgs if total_msgs > 0 else 0

    # -- 7. Reddit funnel conversion --
    funnel_pitched = 0
    funnel_converted = 0
    try:
        import sqlite3
        db_path = Path(r"C:\Users\groot\heather-reddit\reddit_chat.db")
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cur = conn.execute(
                "SELECT status, COUNT(*) FROM conversations GROUP BY status"
            )
            for status, cnt in cur.fetchall():
                if status == "telegram_pitched":
                    funnel_pitched = cnt
                elif status == "converted":
                    funnel_converted = cnt
            conn.close()
    except Exception:
        pass

    return {
        "hours": hours,
        "cutoff_str": cutoff_str,
        "total_msgs": total_msgs,
        "unique_users": len(users),
        "disclosures": disclosures,
        "engaged": engaged,
        "casual": casual,
        "latencies": latencies,
        "slow": slow,
        "errors": errors,
        "truncations": truncations,
        "gender": gender,
        "incomplete": incomplete,
        "img_gen": img_gen,
        "img_anatomy": img_anatomy,
        "img_lib": img_lib,
        "img_cap": img_cap,
        "img_fail": img_fail,
        "vids": vids,
        "vid_accepts": vid_accepts,
        "stories": stories,
        "pos_count": pos_count,
        "neg_count": neg_count,
        "neg_examples": neg_examples,
        "dissatisfaction": dissatisfaction,
        "bye_users": bye_users,
        "steered": steered,
        "csam": csam,
        "csam_users": csam_users,
        "users": users,
        "breeding_user": breeding_user,
        "breeding_bot": breeding_bot,
        "frank_bot": frank_bot,
        "breeding_injections": breeding_injections,
        "source_counts": source_counts,
        # New metrics
        "returning_users": returning_users,
        "new_users_chatting": new_users_chatting,
        "avg_depth": avg_depth,
        "tip_signals": tip_signals,
        "payment_starts": payment_starts,
        "voice_requests": voice_requests,
        "voice_failures": voice_failures,
        "hour_buckets": hour_buckets,
        "photo_chat_ratio": photo_chat_ratio,
        "total_photos": total_photos,
        "funnel_pitched": funnel_pitched,
        "funnel_converted": funnel_converted,
    }


# -- Report Builder --------------------------------------------------------
def build_report(m: dict) -> str:
    """Build the text report from parsed metrics."""
    lines = []
    h = m["hours"]
    lines.append(f"<b>HEATHERBOT {h}HR REPORT</b>")
    lines.append(f"Window: {m['cutoff_str']} -> now")
    lines.append("")

    # Volume & Health
    lines.append("<b>-- VOLUME &amp; HEALTH --</b>")
    lines.append(f"Messages: {m['total_msgs']} from {m['unique_users']} users")
    lines.append(f"New users: {m['disclosures']} | Returning: {m['returning_users']} | First-timers chatting: {m['new_users_chatting']}")
    lines.append(f"Engaged (10+): {len(m['engaged'])} ({sum(m['engaged'].values())} msgs) | Avg depth: {m['avg_depth']:.1f} msgs/user")
    lines.append(f"Casual (&lt;10): {len(m['casual'])} ({sum(m['casual'].values())} msgs)")
    if m["latencies"]:
        avg_l = sum(m["latencies"]) / len(m["latencies"])
        med_l = sorted(m["latencies"])[len(m["latencies"]) // 2]
        max_l = max(m["latencies"])
        lines.append(f"Latency: avg {avg_l:.2f}s, med {med_l:.2f}s, max {max_l:.1f}s")
        lines.append(f"Replies: {len(m['latencies'])} | Slow (&gt;5s): {m['slow']}")
    lines.append(f"Errors: {len(m['errors'])} | Truncations: {m['truncations']} | Gender: {m['gender']}")
    lines.append("")

    # Top chatters
    lines.append("<b>-- TOP CHATTERS --</b>")
    for u, c in m["users"].most_common(8):
        lines.append(f"  {c:3d} msgs  {u}")
    lines.append("")

    # Activity heatmap
    if m["hour_buckets"]:
        lines.append("<b>-- ACTIVITY HEATMAP --</b>")
        for bucket in sorted(m["hour_buckets"].keys()):
            cnt = m["hour_buckets"][bucket]
            bar = "#" * min(cnt // 3, 20)  # scale bar
            lines.append(f"  {bucket}: {cnt:3d} msgs {bar}")
        lines.append("")

    # Content delivery
    lines.append("<b>-- CONTENT DELIVERY --</b>")
    lines.append(f"FLUX gen: {m['img_gen']} (anatomy: {m['img_anatomy']})")
    lines.append(f"Library imgs: {m['img_lib']} | Cap declines: {m['img_cap']} | Failures: {m['img_fail']}")
    lines.append(f"Total photos: {m['total_photos']} | Photo/chat ratio: {m['photo_chat_ratio']:.1%}")
    lines.append(f"Videos: {m['vids']} (offers: {m['vid_accepts']}) | Stories: {m['stories']}")
    lines.append(f"Voice notes: {m['voice_requests']} sent | {m['voice_failures']} failed")
    lines.append("")

    # Sentiment
    pos, neg = m["pos_count"], m["neg_count"]
    ratio = f"{pos/neg:.1f}:1" if neg > 0 else f"{pos}:0"
    lines.append("<b>-- SENTIMENT --</b>")
    lines.append(f"Positive: {pos} | Negative: {neg} | Ratio: {ratio}")
    if m["neg_examples"]:
        for ne in m["neg_examples"][:3]:
            lines.append(f"  {ne}")
    lines.append(f"Dissatisfaction: {len(m['dissatisfaction'])} | Goodbyes: {len(m['bye_users'])}")
    lines.append(f"Steering: {m['steered']}")
    lines.append("")

    # Revenue
    lines.append("<b>-- REVENUE --</b>")
    lines.append(f"Tip signals: {m['tip_signals']} | Payment bot starts: {m['payment_starts']}")
    lines.append("")

    # Traffic sources & funnel
    lines.append("<b>-- TRAFFIC &amp; FUNNEL --</b>")
    if m["source_counts"]:
        src_parts = [f"{src}: {cnt}" for src, cnt in m["source_counts"].most_common()]
        lines.append(f"New user sources: {' | '.join(src_parts)}")
    lines.append(f"Reddit funnel: {m['funnel_pitched']} pitched | {m['funnel_converted']} converted")
    lines.append("")

    # CSAM
    lines.append("<b>-- CSAM FLAGS --</b>")
    lines.append(f"Total: {len(m['csam'])}")
    for u, c in m["csam_users"].most_common():
        flag = " !! REVIEW" if c >= CSAM_FLAG_THRESHOLD else ""
        lines.append(f"  {c}x  {u}{flag}")
    lines.append("")

    # Breeding / CNC
    lines.append("<b>-- BREEDING/CNC --</b>")
    lines.append(f"Injections fired: {m['breeding_injections']}")
    lines.append(f"User msgs: {len(m['breeding_user'])} | Bot replies: {len(m['breeding_bot'])} | Frank mentions: {len(m['frank_bot'])}")
    if m["breeding_bot"][:5]:
        for b in m["breeding_bot"][:5]:
            lines.append(f"  {b[:100]}")
    lines.append("")

    return "\n".join(lines)


# -- Issue Detection & Auto-Fix --------------------------------------------
def detect_issues(m: dict, dry_run: bool = False) -> tuple:
    """Detect issues and apply safe auto-fixes. Returns (issues, fixes)."""
    issues = []
    fixes_applied = []

    # 1. Truncation rate check
    reply_count = len(m["latencies"]) if m["latencies"] else 1
    trunc_rate = m["truncations"] / reply_count if reply_count > 0 else 0
    if trunc_rate > TRUNCATION_RATE_THRESHOLD:
        issues.append(
            f"High truncation rate: {m['truncations']}/{reply_count} "
            f"({trunc_rate:.1%}) -- threshold is {TRUNCATION_RATE_THRESHOLD:.0%}"
        )
        if not dry_run:
            fix = bump_max_tokens(BOT_SCRIPT)
            if fix:
                fixes_applied.append(fix)

    # 2. CSAM repeat offenders -- flag for admin review (NO auto-block)
    for u, c in m["csam_users"].most_common():
        if c >= CSAM_FLAG_THRESHOLD:
            id_match = re.search(r"\((\d+)\)", u)
            uid = id_match.group(1) if id_match else "?"
            issues.append(
                f"CSAM repeat offender: {u} -- {c} flags in {m['hours']}hrs. "
                f"Review and /admin_block {uid} if needed."
            )

    # 3. Latency spike
    if m["latencies"] and max(m["latencies"]) > LATENCY_SPIKE_THRESHOLD:
        issues.append(
            f"Latency spike: max {max(m['latencies']):.1f}s "
            f"(threshold: {LATENCY_SPIKE_THRESHOLD}s)"
        )

    # 4. Error count
    if len(m["errors"]) > ERROR_THRESHOLD:
        issues.append(f"{len(m['errors'])} errors in {m['hours']}hrs")
        for e in m["errors"][:3]:
            issues.append(f"  {e[:120]}")

    # 5. Image generation failures
    if m["img_fail"] > 0:
        issues.append(f"{m['img_fail']} image generation failures")

    # 6. Zero breeding bot replies despite injections
    if m["breeding_injections"] > 5 and len(m["breeding_bot"]) == 0:
        issues.append(
            f"Breeding injection fired {m['breeding_injections']}x but bot "
            f"produced 0 breeding replies. LLM may be ignoring the prompt."
        )

    # 7. High cap declines vs low generations
    if m["img_cap"] > 0 and m["img_gen"] > 0 and m["img_cap"] / m["img_gen"] > 3:
        issues.append(
            f"Photo cap too restrictive: {m['img_cap']} declines vs "
            f"{m['img_gen']} generations ({m['img_cap']/m['img_gen']:.1f}x ratio)"
        )

    # 8. Voice failures
    if m["voice_failures"] > 3:
        issues.append(f"Voice note failures: {m['voice_failures']} "
                      f"(of {m['voice_requests']} requests)")

    # 9. Low retention (if enough data)
    if m["unique_users"] >= 10 and m["returning_users"] == 0:
        issues.append("Zero returning users -- all traffic is new. "
                      "Retention may be an issue.")

    return issues, fixes_applied


def bump_max_tokens(bot_file: Path) -> str | None:
    """Bump all max_tokens floors by 10 to reduce truncations."""
    code = bot_file.read_text(encoding="utf-8")

    pattern = re.compile(r"(max_tokens\s*=\s*random\.randint\()(\d+)(,\s*)(\d+)(\))")
    matches = list(pattern.finditer(code))

    if not matches:
        return None

    floors = [int(m.group(2)) for m in matches]
    if sum(floors) / len(floors) > 150:
        log_change("SKIPPED max_tokens bump -- average floor already > 150")
        return None

    new_code = code
    for m in reversed(matches):
        old_floor = int(m.group(2))
        old_ceil = int(m.group(4))
        new_floor = old_floor + 10
        new_ceil = old_ceil + 10
        replacement = f"{m.group(1)}{new_floor}{m.group(3)}{new_ceil}{m.group(5)}"
        new_code = new_code[:m.start()] + replacement + new_code[m.end():]

    if new_code != code:
        bot_file.write_text(new_code, encoding="utf-8")
        msg = (f"AUTO-FIX: Bumped {len(matches)} max_tokens ranges by +10 "
               f"(truncation rate exceeded threshold). RESTART NEEDED.")
        log_change(msg)
        return msg

    return None


# -- Change Notification ---------------------------------------------------
def check_and_notify_changes():
    """Check auto_changes.log for new entries since last notification.
    Sends a Telegram alert to admin with the new changes and restart status."""
    if not CHANGES_LOG.exists():
        print("No changes log found.")
        return

    all_lines = CHANGES_LOG.read_text(encoding="utf-8").splitlines()
    if not all_lines:
        print("Changes log is empty.")
        return

    # Read last notified position
    last_pos = 0
    if CHANGES_NOTIFIED.exists():
        try:
            last_pos = int(CHANGES_NOTIFIED.read_text().strip())
        except (ValueError, OSError):
            last_pos = 0

    new_lines = all_lines[last_pos:]
    if not new_lines:
        print("No new changes since last notification.")
        return

    # Check if any changes require restart
    needs_restart = any("RESTART NEEDED" in l or "AUTO-FIX" in l or
                        "max_tokens" in l or "code change" in l.lower()
                        for l in new_lines)

    msg_parts = [f"<b>CODE CHANGES ({len(new_lines)} new)</b>\n"]
    for line in new_lines[-15:]:  # Cap at 15 most recent
        msg_parts.append(line)

    if needs_restart:
        msg_parts.append("")
        msg_parts.append("<b>!! RESTART NEEDED !!</b>")
        msg_parts.append("Run: <code>taskkill /PID [bot_pid] /F</code>")
        msg_parts.append("Then: <code>cd heather-bot &amp;&amp; python kelly_telegram_bot.py --monitoring --small-model --personality kelly_persona.yaml --log-dir C:\\AI\\logs</code>")

    send_telegram("\n".join(msg_parts))
    print(f"Notified admin of {len(new_lines)} new changes (restart={needs_restart})")

    # Update position
    CHANGES_NOTIFIED.write_text(str(len(all_lines)))


# -- Main ------------------------------------------------------------------
def main():
    args = sys.argv[1:]
    dry_run = "--dry-run" in args
    notify_only = "--notify-changes" in args

    # If just checking for code changes, do that and exit
    if notify_only:
        check_and_notify_changes()
        return

    hours = 12
    for a in args:
        if a.isdigit():
            hours = int(a)

    # Ensure output dirs exist
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    CHANGES_LOG.parent.mkdir(parents=True, exist_ok=True)

    print(f"Generating {hours}hr report (dry_run={dry_run})...")

    # Parse log
    metrics = parse_log(hours)

    # Build report
    report = build_report(metrics)

    # Detect issues
    issues, fixes = detect_issues(metrics, dry_run=dry_run)

    # Append issues to report
    if issues or fixes:
        report += "<b>-- ISSUES &amp; ACTIONS --</b>\n"
        for issue in issues:
            report += f"  {issue}\n"
        for fix in fixes:
            report += f"  [FIXED] {fix}\n"
    else:
        report += "<b>-- STATUS --</b>\n  All clear -- no issues detected.\n"

    # If fixes were applied, add restart banner
    if fixes:
        report += "\n<b>!! BOT RESTART NEEDED !!</b>\n"
        report += "Code changes were applied. Restart the bot to activate.\n"

    # Save report to disk
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    report_file = REPORTS_DIR / f"report_{ts}_{hours}hr.txt"
    plain = re.sub(r"<[^>]+>", "", report)
    report_file.write_text(plain, encoding="utf-8")
    print(f"Report saved: {report_file}")

    # Send to admin via Telegram
    if not dry_run:
        print("Sending report to admin via Telegram...")
        ok = send_telegram(report)
        if ok:
            print("Report sent successfully.")
            log_change(f"Sent {hours}hr report to admin "
                       f"({metrics['total_msgs']} msgs, {metrics['unique_users']} users)")
        else:
            print("WARNING: Failed to send report.")
    else:
        print("DRY RUN -- not sending to Telegram.")
        print("\n" + plain)

    # Log and notify about any fixes
    if fixes:
        print(f"\nAuto-fixes applied ({len(fixes)}):")
        for f_msg in fixes:
            print(f"  {f_msg}")
        # Send separate urgent notification about restart
        restart_msg = (
            "<b>!! AUTO-FIX APPLIED -- RESTART NEEDED !!</b>\n\n"
            + "\n".join(fixes)
            + "\n\nBot is running old code until restarted."
        )
        if not dry_run:
            send_telegram(restart_msg)


if __name__ == "__main__":
    main()
