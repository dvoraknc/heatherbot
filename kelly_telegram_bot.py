#!/usr/bin/env python3
"""Kelly bot entrypoint wrapper for the legacy heather_telegram_bot module."""

from pathlib import Path
import runpy


if __name__ == "__main__":
    base_dir = Path(__file__).resolve().parent
    runpy.run_path(str(base_dir / "heather_telegram_bot.py"), run_name="__main__")
