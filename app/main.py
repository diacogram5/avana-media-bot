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
    try:
        asyncio.run(run_bot(config))
    except KeyboardInterrupt:
        print("Bot stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
