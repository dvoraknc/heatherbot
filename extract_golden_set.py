"""Extract a Golden Set of examples from Heather Bot logs for eval/fine-tuning.

Usage:
    python extract_golden_set.py [--log chat_logs/recent_log_dump.txt] [--out golden_set.jsonl]

Parses the log dump to extract user→Heather turn pairs, scores them on quality
metrics, and outputs the top 50 good + 20 bad examples as JSONL.

Quality signals (positive):
  - Response length 20-300 chars (concise, natural)
  - Contains question back to user (engagement)
  - Contains emoji (personality)
  - Latency under 30s
  - Uses pet names / Heather-style language

Quality signals (negative / "bad" examples):
  - Character violations (safety refusal phrases)
  - Gender violations
  - Very short (<15 chars) or very long (>500 chars)
  - Contains phantom photo claims
  - Contains human life claims (physical activity hallucinations)
"""
import re
import json
import argparse
import os
from collections import defaultdict
from typing import Optional

# Heather-style markers
PET_NAMES = {'babe', 'baby', 'sweetie', 'hun', 'honey', 'handsome', 'cutie', 'sailor', 'sugar'}
SAFETY_REFUSAL_PHRASES = [
    # Mirrors AI_SAFETY_REFUSAL_PHRASES from heather_telegram_bot.py
    'i am a language model', 'i\'m a language model', 'i was programmed',
    'virtual assistant', 'trained by', 'anthropic', 'claude',
    'openai', 'gpt', 'language model', 'my training', 'my programming',
    'i can\'t continue', 'i can\'t engage', 'i can\'t generate',
    'i\'m not comfortable', 'not comfortable engaging',
    'sexually explicit', 'explicit content', 'explicit roleplay',
    'i must respectfully', 'i need to decline', 'i\'m unable to',
    'against my guidelines', 'content policy', 'safety guidelines',
    'as a responsible', 'i apologize but', 'i\'m sorry but i can\'t',
    'i cannot fulfill', 'i cannot participate', 'i\'m not able to',
    'i won\'t be able to', 'i can not', 'i am not able',
    'appropriate boundaries', 'maintain appropriate', 'uncomfortable with',
    'beyond what i can', 'outside my capabilities', 'not something i can',
    'ethical guidelines', 'ethical boundaries', 'professional boundaries',
    'i have to decline', 'i have to refuse', 'i must decline',
    'inappropriate content', 'harmful content',
]
GENDER_VIOLATION_PHRASES = [
    'my cock', 'my dick', 'my penis', 'my erection', 'my shaft',
    'i\'m hard', 'rock hard', 'my boner', 'jacking off',
]
HUMAN_LIFE_PHRASES = [
    'just got home', 'at the store', 'my shift', 'uber shift',
    'making dinner', 'taking a shower', 'drinking coffee', 'cooking dinner',
    'doing laundry', 'getting dressed', 'on my way', 'got off work',
    'sipping my', 'eating lunch', 'eating dinner', 'grabbing a bite',
]

# Log line patterns
TEXT_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| INFO\s+\| '
    r'\[R\d+-(\d+)\] Text from (.+?) \((\d+)\) \(chat\): (.+)'
)
REPLY_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| INFO\s+\| '
    r'\[R\d+-(\d+)\] Reply to (\d+) \((\d+\.\d+)s\): (.+)'
)
VIOLATION_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| WARNING\s+\| '
    r'(Character violation|Gender violation)'
)
STEERING_RE = re.compile(
    r'(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \| INFO\s+\| '
    r'STEERING cue for (\d+): (.+)'
)


def parse_log(filepath: str) -> list:
    """Parse log file and extract turn pairs with metadata."""
    # Collect all messages keyed by chat_id
    user_msgs = {}  # chat_id -> list of (timestamp, seq, display_name, text)
    bot_replies = {}  # chat_id -> list of (timestamp, seq, latency, text)
    violations = []  # list of (timestamp, type)
    steering = {}  # chat_id -> list of cue texts

    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        for line in f:
            line = line.rstrip()

            m = TEXT_RE.match(line)
            if m:
                ts, seq, display, chat_id, text = m.groups()
                chat_id = int(chat_id)
                if chat_id not in user_msgs:
                    user_msgs[chat_id] = []
                user_msgs[chat_id].append((ts, int(seq), display, text))
                continue

            m = REPLY_RE.match(line)
            if m:
                ts, seq, chat_id, latency, text = m.groups()
                chat_id = int(chat_id)
                if chat_id not in bot_replies:
                    bot_replies[chat_id] = []
                bot_replies[chat_id].append((ts, int(seq), float(latency), text))
                continue

            m = VIOLATION_RE.match(line)
            if m:
                ts, vtype = m.groups()
                violations.append((ts, vtype))
                continue

            m = STEERING_RE.match(line)
            if m:
                ts, chat_id, cue = m.groups()
                chat_id = int(chat_id)
                if chat_id not in steering:
                    steering[chat_id] = []
                steering[chat_id].append(cue)

    # Match user messages to bot replies by chat_id and sequence proximity
    pairs = []
    for chat_id in user_msgs:
        if chat_id not in bot_replies:
            continue
        u_msgs = sorted(user_msgs[chat_id], key=lambda x: x[1])
        b_msgs = sorted(bot_replies[chat_id], key=lambda x: x[1])

        b_idx = 0
        for u_ts, u_seq, display, u_text in u_msgs:
            # Find the next bot reply with seq > u_seq
            while b_idx < len(b_msgs) and b_msgs[b_idx][1] <= u_seq:
                b_idx += 1
            if b_idx < len(b_msgs):
                b_ts, b_seq, latency, b_text = b_msgs[b_idx]
                # Only pair if seq gap is small (within 30 sequence numbers)
                if b_seq - u_seq <= 30:
                    pairs.append({
                        'chat_id': chat_id,
                        'display_name': display,
                        'user_message': u_text,
                        'bot_response': b_text,
                        'latency': latency,
                        'timestamp': u_ts,
                        'had_steering': chat_id in steering,
                    })
                    b_idx += 1

    return pairs


def score_pair(pair: dict) -> dict:
    """Score a turn pair on quality metrics. Returns pair with score and tags."""
    score = 0.0
    tags = []
    resp = pair['bot_response']
    resp_lower = resp.lower()

    # --- Positive signals ---
    # Good length (20-300 chars)
    rlen = len(resp)
    if 20 <= rlen <= 300:
        score += 1.0
        tags.append('good_length')
    elif rlen > 300:
        score -= 0.5
        tags.append('long')
    elif rlen < 15:
        score -= 2.0
        tags.append('too_short')

    # Contains a question (engagement)
    if '?' in resp:
        score += 1.0
        tags.append('asks_question')

    # Contains emoji
    if re.search(r'[\U0001F600-\U0001F9FF]|😏|😘|😉|😜|🥺|💕|☕|😈|🔥', resp):
        score += 0.5
        tags.append('has_emoji')

    # Uses pet names
    if any(pn in resp_lower for pn in PET_NAMES):
        score += 0.5
        tags.append('pet_name')

    # Good latency
    if pair['latency'] < 30:
        score += 0.5
        tags.append('fast')
    elif pair['latency'] > 60:
        score -= 0.5
        tags.append('slow')

    # Ends with casual closer (lol, etc.)
    if re.search(r'\b(lol|haha|tbh|tho|ngl)\b', resp_lower):
        score += 0.3
        tags.append('casual_tone')

    # --- Negative signals ---
    # Safety refusal
    if any(p in resp_lower for p in SAFETY_REFUSAL_PHRASES):
        score -= 5.0
        tags.append('safety_refusal')

    # Gender violation
    if any(p in resp_lower for p in GENDER_VIOLATION_PHRASES):
        score -= 5.0
        tags.append('gender_violation')

    # Human life claims
    if any(p in resp_lower for p in HUMAN_LIFE_PHRASES):
        score -= 2.0
        tags.append('human_life_claim')

    # Phantom photo claims
    if re.search(r'(?:just )?sent (?:you )?(?:a )?(?:pic|photo|selfie)', resp_lower):
        score -= 2.0
        tags.append('phantom_photo')

    pair['score'] = round(score, 1)
    pair['tags'] = tags
    return pair


def load_pre_tagged(filepath: str) -> list:
    """Load a pre-tagged conversation file (chad_the_flirt.txt format)."""
    pairs = []
    with open(filepath, 'r', encoding='utf-8', errors='replace') as f:
        content = f.read()

    # Extract header for persona name
    header_m = re.search(r'CONVERSATION: (\w+)', content)
    persona = header_m.group(1) if header_m else os.path.basename(filepath)

    # Parse turns — each block is "--- Turn N ... ---\n  Name: ...\n  Heather: ..."
    turn_re = re.compile(
        r'--- Turn (\d+).*?---\s*\n'
        r'\s+.+?: (.+)\n'
        r'\s+Heather: (.+?)(?:\s+\[([^\]]+)\])?\s*$',
        re.MULTILINE
    )
    for m in turn_re.finditer(content):
        turn_num, user_msg, bot_resp, tag = m.groups()
        pairs.append({
            'chat_id': 0,
            'display_name': persona,
            'user_message': user_msg.strip(),
            'bot_response': bot_resp.strip(),
            'latency': 0.0,
            'timestamp': '',
            'had_steering': False,
            'source': f'pre_tagged:{persona}',
            'pre_tag': tag,
        })

    return pairs


def main():
    parser = argparse.ArgumentParser(description='Extract Golden Set from Heather Bot logs')
    parser.add_argument('--log', type=str, default='chat_logs/recent_log_dump.txt',
                        help='Log dump file to parse')
    parser.add_argument('--chat-logs-dir', type=str, default='chat_logs',
                        help='Directory with pre-tagged conversation files')
    parser.add_argument('--out', type=str, default='golden_set.jsonl',
                        help='Output JSONL file')
    parser.add_argument('--good', type=int, default=50, help='Number of good examples')
    parser.add_argument('--bad', type=int, default=20, help='Number of bad examples')
    args = parser.parse_args()

    # Change to script directory for relative paths
    os.chdir(os.path.dirname(os.path.abspath(__file__)))

    all_pairs = []

    # Parse main log dump
    if os.path.exists(args.log):
        print(f"Parsing {args.log}...")
        log_pairs = parse_log(args.log)
        for p in log_pairs:
            p['source'] = 'log_dump'
        all_pairs.extend(log_pairs)
        print(f"  Found {len(log_pairs)} turn pairs")
    else:
        print(f"Warning: {args.log} not found, skipping")

    # Parse pre-tagged files
    if os.path.isdir(args.chat_logs_dir):
        for fname in os.listdir(args.chat_logs_dir):
            if fname.endswith('.txt') and fname != 'recent_log_dump.txt' and fname != 'last6h.txt':
                fpath = os.path.join(args.chat_logs_dir, fname)
                tagged = load_pre_tagged(fpath)
                all_pairs.extend(tagged)
                if tagged:
                    print(f"  Loaded {len(tagged)} turns from {fname}")

    print(f"Total pairs: {len(all_pairs)}")

    # Score all pairs
    scored = [score_pair(p) for p in all_pairs]

    # Deduplicate by (user_message, bot_response)
    seen = set()
    unique = []
    for p in scored:
        key = (p['user_message'][:80], p['bot_response'][:80])
        if key not in seen:
            seen.add(key)
            unique.append(p)
    print(f"After dedup: {len(unique)} unique pairs")

    # Split into good and bad
    good = sorted([p for p in unique if p['score'] > 0], key=lambda x: -x['score'])
    bad = sorted([p for p in unique if p['score'] <= 0], key=lambda x: x['score'])

    selected_good = good[:args.good]
    selected_bad = bad[:args.bad]

    print(f"Selected: {len(selected_good)} good (score range {selected_good[0]['score'] if selected_good else 0}..{selected_good[-1]['score'] if selected_good else 0})")
    print(f"Selected: {len(selected_bad)} bad (score range {selected_bad[0]['score'] if selected_bad else 0}..{selected_bad[-1]['score'] if selected_bad else 0})")

    # Write output
    output = []
    for p in selected_good:
        output.append({
            'user': p['user_message'],
            'assistant': p['bot_response'],
            'score': p['score'],
            'tags': p['tags'],
            'label': 'good',
            'source': p.get('source', 'unknown'),
            'chat_id': p['chat_id'],
            'latency': p['latency'],
        })
    for p in selected_bad:
        output.append({
            'user': p['user_message'],
            'assistant': p['bot_response'],
            'score': p['score'],
            'tags': p['tags'],
            'label': 'bad',
            'source': p.get('source', 'unknown'),
            'chat_id': p['chat_id'],
            'latency': p['latency'],
        })

    with open(args.out, 'w', encoding='utf-8') as f:
        for item in output:
            f.write(json.dumps(item, ensure_ascii=False) + '\n')

    print(f"\nWrote {len(output)} examples to {args.out}")

    # Print sample (safe for Windows console)
    def safe(s):
        return s.encode('ascii', 'replace').decode('ascii')

    print("\n--- Top 3 Good ---")
    for p in selected_good[:3]:
        print(f"  [{p['score']}] {p['tags']}")
        print(f"  User: {safe(p['user_message'][:80])}")
        print(f"  Bot:  {safe(p['bot_response'][:80])}")
        print()

    print("--- Top 3 Bad ---")
    for p in selected_bad[:3]:
        print(f"  [{p['score']}] {p['tags']}")
        print(f"  User: {safe(p['user_message'][:80])}")
        print(f"  Bot:  {safe(p['bot_response'][:80])}")
        print()


if __name__ == '__main__':
    main()
