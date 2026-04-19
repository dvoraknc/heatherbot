#!/usr/bin/env python3
"""
Simple Telethon test - verifies userbot login works
"""
from telethon import TelegramClient
import asyncio
import os

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

# API credentials from my.telegram.org (set in .env)
API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
API_HASH = os.getenv("TELEGRAM_API_HASH", "")

# Session file will be created in current directory
SESSION_NAME = "kelly_session"

async def main():
    print("Starting Telethon test...")
    print("=" * 50)

    # Create the client
    client = TelegramClient(SESSION_NAME, API_ID, API_HASH)

    # Connect and login (will prompt for phone + code on first run)
    await client.start()

    # Get info about the logged-in account
    me = await client.get_me()
    print("\n✅ Successfully logged in!")
    print(f"   Name: {me.first_name} {me.last_name or ''}")
    print(f"   Username: @{me.username}" if me.username else "   Username: (none)")
    print(f"   Phone: {me.phone}")
    print(f"   User ID: {me.id}")

    # Send a test message to Saved Messages (yourself)
    print("\n📤 Sending test message to Saved Messages...")
    await client.send_message("me", "🤖 Telethon userbot test successful!")
    print("   ✅ Message sent! Check your Saved Messages in Telegram.")

    # Test receiving - get last 3 messages from Saved Messages
    print("\n📥 Reading recent Saved Messages...")
    async for message in client.iter_messages("me", limit=3):
        preview = message.text[:50] + "..." if message.text and len(message.text) > 50 else message.text
        print(f"   - {preview}")

    print("\n" + "=" * 50)
    print("✅ All tests passed! Telethon userbot is working.")
    print("   Session saved to: kelly_session.session")
    print("   (Future runs won't need phone verification)")
    print("=" * 50)

    await client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())
