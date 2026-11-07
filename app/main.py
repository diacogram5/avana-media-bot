"""Entry point: ``python -m app.main`` (run from the project root).

Exits with a clear message (not a traceback) when BOT_TOKEN is missing.
Graceful shutdown: polling stops on Ctrl+C and the DB connection closes.
"""
from __future__ import annotations

import asyncio
import sys

from .bot.handlers import run_bot
from .config import get_config


def main() -> int:
    config = get_config()
    if not config.has_token:
        print("خطا: BOT_TOKEN تنظیم نشده است. فایل .env را از روی .env.example بسازید و توکن BotFather را وارد کنید.")
        # Exit non-zero so Render restarts and the dead-bot state is
        # visible instead of reporting the service Live.
        return 2
    try:
        asyncio.run(run_bot(config))
    except KeyboardInterrupt:
        print("Bot stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
