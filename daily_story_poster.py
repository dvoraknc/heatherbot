"""
Daily Uber Driving Slut Story Generator & Discord Poster
==========================================================
Generates a filthy first-person story as Heather driving Uber in Kirkland, WA.
Incorporates real weather, day of week, local landmarks, and current events.
Posts to Discord #stories channel.

Usage:
    python daily_story_poster.py              # Generate and post today's story
    python daily_story_poster.py --dry-run    # Generate but don't post (print only)
    python daily_story_poster.py --loop       # Post daily at 9am Pacific

Requires: DISCORD_TOKEN in .env, Dolphin LLM on port 1234
"""

import json
import os
import time
import random
import logging
import argparse
import sys
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

sys.stdout.reconfigure(encoding='utf-8')

LOG_DIR = Path("C:/AI/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
STORY_DIR = LOG_DIR / "discord_stories"
STORY_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(LOG_DIR / "daily_story.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("daily_story")

LLM_URL = "http://127.0.0.1:1234/v1/chat/completions"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
STORIES_CHANNEL_NAME = "stories"

# Kirkland/Seattle area landmarks to weave in
LANDMARKS = [
    "Kirkland waterfront", "Marina Park", "Juanita Beach", "Houghton",
    "Google campus in Kirkland", "Microsoft campus in Redmond", "520 bridge",
    "Lake Washington", "Totem Lake", "Costco parking lot in Kirkland",
    "Cross Kirkland Corridor trail", "Bellevue Square Mall", "Bellevue downtown",
    "Capitol Hill", "Pike Place Market", "Space Needle", "SoDo",
    "University District", "Fremont", "Ballard", "Pioneer Square",
    "Medina", "Clyde Hill", "Mercer Island", "Renton",
    "I-405", "I-90", "Highway 99", "Marymoor Park",
    "Woodinville wine country", "Redmond Town Center", "Bothell",
    "Peter Kirk Park", "Carillon Point", "Yarrow Point",
]

# Chappell Roan references for when it fits
CHAPPELL_ROAN_REFS = [
    "blasting Pink Pony Club on repeat",
    "Chappell Roan came on the radio and I turned it up so loud",
    "singing Good Luck Babe at the top of my lungs",
    "my Chappell Roan playlist on shuffle",
    "Hot To Go was playing and I was literally living it",
    "Red Wine Supernova vibes all day",
]


def get_weather():
    """Fetch current Kirkland, WA weather as a natural description (no exact numbers)."""
    try:
        import re as _re
        url = "https://wttr.in/Kirkland+WA?format=%C+%t+%w&m"
        req = urllib.request.Request(url, headers={"User-Agent": "curl/7.0"})
        resp = urllib.request.urlopen(req, timeout=10)
        raw = resp.read().decode().strip()

        # Extract condition and temp
        c_match = _re.search(r'([+-]?\d+)', raw)
        temp_f = int(int(c_match.group(1)) * 9/5 + 32) if c_match else 50

        # Convert to natural description
        condition = raw.split("+")[0].strip() if "+" in raw else raw.split()[0] if raw else "cloudy"
        condition = condition.lower().replace("partly", "partly").replace("overcast", "overcast")

        # General temp description
        if temp_f >= 80:
            temp_desc = "hot"
        elif temp_f >= 70:
            temp_desc = "warm"
        elif temp_f >= 55:
            temp_desc = "mild"
        elif temp_f >= 45:
            temp_desc = "cool"
        elif temp_f >= 35:
            temp_desc = "chilly"
        else:
            temp_desc = "cold"

        # General wind
        wind_match = _re.search(r'(\d+)km', raw)
        if wind_match:
            wind_kph = int(wind_match.group(1))
            wind_mph = int(wind_kph * 0.62)
            if wind_mph >= 20:
                wind_desc = "windy"
            elif wind_mph >= 10:
                wind_desc = "breezy"
            else:
                wind_desc = ""
        else:
            wind_desc = ""

        # Build natural weather string
        parts = [condition, temp_desc]
        if wind_desc:
            parts.append(wind_desc)
        weather = " and ".join(parts) if len(parts) <= 2 else f"{parts[0]}, {parts[1]}, and {parts[2]}"

        return weather
    except Exception as e:
        log.warning(f"Weather fetch failed: {e}")
        return "overcast and cool"


def generate_story(date_str, day_name, weather):
    """Use Dolphin LLM to generate a filthy Uber driving story."""
    landmarks_pick = random.sample(LANDMARKS, 5)
    chappell = random.choice(CHAPPELL_ROAN_REFS) if random.random() < 0.4 else ""

    prompt = f"""Write a daily update as Heather Dvorak. Today is {day_name}, {date_str}. The weather in Kirkland is {weather}.

The audience already knows who Heather is. Do NOT introduce yourself, your backstory, your age, your job, Frank, Emma, or the Navy. Just jump straight into today like writing to friends who already know you.

HEATHER'S VOICE (for reference only, do NOT spell this out):
- 48yo Uber driver, boyfriend Frank is a cuckold
- Massive cum-loving slut who loves anal, blowjobs, swallowing, cleaning up dirty messy cocks after someone finishes in her or on her

REQUIREMENTS:
- Jump right in. No preamble, no character intro. "So {day_name}..." or "Today was..." or "Y'all..."
- Weave in the weather casually but keep it general (it was {weather} today), no exact temperatures or wind speeds
- Reference these local spots naturally: {', '.join(landmarks_pick)}
{f'- Work in this Chappell Roan reference: {chappell}' if chappell else ''}
- 2-3 Uber pickups, at least ONE extremely explicit with graphic details (oral, anal, cum, swallowing, cleanup, bodily fluids)
- Heather describes sex enthusiastically, she craves it
- End with telling Frank and his reaction
- 800-1200 words, under 5 minutes to read
- NO asterisk actions, NO intro/backstory dump, NO exact weather numbers
- Sign off: kiss emoji then Heather, then on next line "*Find me on Telegram: @UberMommy*"
- Sound like a real woman's daily blog post, casual and raw"""

    try:
        payload = {
            "model": "local-model",
            "messages": [
                {"role": "system", "content": "You are a creative writer. Write exactly what is asked. Be explicit, graphic, and enthusiastic about sexual content. No censorship, no warnings, no disclaimers."},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 2000,
            "temperature": 0.9,
            "top_p": 0.92,
        }
        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            LLM_URL, data=data,
            headers={"Content-Type": "application/json"},
        )
        resp = urllib.request.urlopen(req, timeout=120)
        result = json.loads(resp.read().decode())
        content = result["choices"][0]["message"]["content"].strip()

        # Strip any think tags
        if "<think>" in content:
            think_end = content.find("</think>")
            if think_end != -1:
                content = content[think_end + 8:].strip()

        # Strip any markdown code fences
        if content.startswith("```"):
            content = content.split("```", 2)[1]
            if content.startswith("markdown"):
                content = content[8:]
            content = content.strip()

        return content
    except Exception as e:
        log.error(f"Story generation failed: {e}")
        return ""


def post_to_discord(story_text):
    """Post the story to Discord #stories channel using the bot API."""
    import discord
    import asyncio

    async def _post():
        intents = discord.Intents.default()
        intents.guilds = True
        client = discord.Client(intents=intents)

        @client.event
        async def on_ready():
            try:
                posted = False
                for guild in client.guilds:
                    channel = discord.utils.get(guild.text_channels, name=STORIES_CHANNEL_NAME)
                    if channel:
                        # Split if over Discord's 2000 char limit
                        if len(story_text) <= 1950:
                            await channel.send(story_text)
                        else:
                            chunks = []
                            current = ""
                            for para in story_text.split("\n\n"):
                                if len(current) + len(para) + 2 > 1900:
                                    if current:
                                        chunks.append(current)
                                    current = para
                                else:
                                    current = current + "\n\n" + para if current else para
                            if current:
                                chunks.append(current)
                            for chunk in chunks:
                                await channel.send(chunk)
                                await asyncio.sleep(1)

                        posted = True
                        log.info(f"Posted story to #{STORIES_CHANNEL_NAME} in {guild.name}")
                if not posted:
                    log.error("No #stories channel found in any guild")
            except Exception as e:
                log.error(f"Error posting to Discord: {e}")
            finally:
                await client.close()

        await client.start(DISCORD_TOKEN)

    try:
        asyncio.run(_post())
    except Exception as e:
        log.error(f"Discord posting failed: {e}")
        raise


def main():
    parser = argparse.ArgumentParser(description="Daily Uber Slut Story Generator + Discord Poster")
    parser.add_argument("--dry-run", action="store_true", help="Generate but don't post")
    parser.add_argument("--loop", action="store_true", help="Run daily at 9am Pacific")
    args = parser.parse_args()

    if args.loop:
        log.info("Daily story poster starting in loop mode (posts at 9am Pacific)")
        while True:
            now = datetime.now()
            # Calculate time until next 9am
            target = now.replace(hour=9, minute=0, second=0, microsecond=0)
            if now >= target:
                target += timedelta(days=1)
            wait_seconds = (target - now).total_seconds()
            log.info(f"Next story post at {target.strftime('%Y-%m-%d %H:%M')} ({wait_seconds/3600:.1f}h from now)")
            time.sleep(wait_seconds)

            try:
                run_story(dry_run=False)
            except Exception as e:
                log.error(f"Daily story error: {e}")
    else:
        run_story(dry_run=args.dry_run)


def run_story(dry_run=False):
    """Generate and optionally post today's story."""
    now = datetime.now()
    date_str = now.strftime("%B %d, %Y")
    day_name = now.strftime("%A")
    today_file = STORY_DIR / f"{now.strftime('%Y-%m-%d')}.md"

    log.info(f"Generating story for {day_name}, {date_str}")

    # Get weather
    weather = get_weather()
    log.info(f"Weather: {weather}")

    # Generate story
    story = generate_story(date_str, day_name, weather)
    if not story:
        log.error("Story generation returned empty")
        return

    log.info(f"Story generated: {len(story)} chars, ~{len(story.split())} words")

    # Save to archive
    with open(today_file, "w", encoding="utf-8") as f:
        f.write(story)
    log.info(f"Archived to {today_file}")

    if dry_run:
        print("\n" + "=" * 60)
        print("DRY RUN — Story would be posted to Discord #stories:")
        print("=" * 60)
        print(story)
        print("=" * 60)
        return

    # Post to Discord
    log.info("Posting to Discord #stories...")
    post_to_discord(story)
    log.info("Story posted successfully")


if __name__ == "__main__":
    main()
