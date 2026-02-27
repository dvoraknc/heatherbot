"""Bulk upload videos to Telegram Saved Messages for Heather Bot caching.

Usage:
    python upload_videos.py [--session heather_session] [--dry-run]

Scans VIDEO_DIR for video files, checks which are already in Saved Messages,
and uploads any missing ones. Uses the same session as the main bot.
"""
import os
import sys
import asyncio
import argparse

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from telethon import TelegramClient

VIDEO_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "videos")
VIDEO_EXTENSIONS = ('.mp4', '.mov', '.avi', '.mkv', '.webm')

parser = argparse.ArgumentParser(description='Upload videos to Saved Messages for Heather Bot')
parser.add_argument('--session', type=str, default='heather_session', help='Telethon session file name')
parser.add_argument('--dry-run', action='store_true', help='List missing videos without uploading')
parser.add_argument('--limit', type=int, default=0, help='Max videos to upload (0 = all)')
args = parser.parse_args()

API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")
if not API_ID or not API_HASH:
    print("ERROR: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set in .env or environment")
    sys.exit(1)

client = TelegramClient(args.session, API_ID, API_HASH)


def get_local_videos() -> list:
    if not os.path.isdir(VIDEO_DIR):
        return []
    return sorted([f for f in os.listdir(VIDEO_DIR)
                   if f.lower().endswith(VIDEO_EXTENSIONS) and os.path.isfile(os.path.join(VIDEO_DIR, f))])


async def main():
    await client.start()
    me = await client.get_me()
    print(f"Logged in as {me.first_name} (ID: {me.id})")

    local = get_local_videos()
    print(f"Found {len(local)} videos in {VIDEO_DIR}")

    if not local:
        print("No videos to upload.")
        return

    # Scan Saved Messages for already-uploaded videos
    print("Scanning Saved Messages (up to 500 messages)...")
    found = set()
    async for msg in client.iter_messages(me.id, limit=500):
        fname = None
        doc = msg.video or msg.document
        if doc:
            for attr in doc.attributes:
                if hasattr(attr, 'file_name') and attr.file_name:
                    fname = attr.file_name
                    break
        if fname:
            found.add(fname.lower())

    missing = [f for f in local if f.lower() not in found]
    already = len(local) - len(missing)
    print(f"Already uploaded: {already}/{len(local)}")
    print(f"Missing: {len(missing)}")

    if not missing:
        print("All videos are already in Saved Messages!")
        return

    if args.dry_run:
        print("\n[DRY RUN] Would upload:")
        for f in missing:
            size_mb = os.path.getsize(os.path.join(VIDEO_DIR, f)) / (1024 * 1024)
            print(f"  {f} ({size_mb:.1f} MB)")
        return

    to_upload = missing[:args.limit] if args.limit > 0 else missing
    print(f"\nUploading {len(to_upload)} videos...")

    uploaded = 0
    failed = 0
    for i, filename in enumerate(to_upload, 1):
        filepath = os.path.join(VIDEO_DIR, filename)
        size_mb = os.path.getsize(filepath) / (1024 * 1024)
        print(f"  [{i}/{len(to_upload)}] {filename} ({size_mb:.1f} MB)...", end=" ", flush=True)
        try:
            await client.send_file(
                me.id, filepath,
                caption=f"[heather-video] {filename}",
                silent=True
            )
            uploaded += 1
            print("OK")
            await asyncio.sleep(3)  # Rate limit between uploads
        except Exception as e:
            failed += 1
            print(f"FAILED: {e}")

    print(f"\nDone: {uploaded} uploaded, {failed} failed, {already + uploaded}/{len(local)} total cached")

with client:
    client.loop.run_until_complete(main())
