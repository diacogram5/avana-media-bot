"""Structured logging that never logs a token, key or password.

Usage: ``logger = get_logger(__name__)`` then ``logger.info("event", extra=...)``.
Secrets are redacted by construction: callers pass ``config.safe_summary()``
instead of the config object, and no handler ever prints message bodies that
may contain a token.
"""
from __future__ import annotations

import json
import logging
import sys

_SENSITIVE_MARKERS = ("BOT_TOKEN", "YOUTUBE_API_KEY", "password", "token=")


def _redact_record(record: logging.LogRecord) -> bool:
    msg = record.getMessage()
    for marker in _SENSITIVE_MARKERS:
        if marker in msg:
            record.msg = "[redacted secret marker]"
            record.args = ()
            break
    return True


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handler.addFilter(_redact_record)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    return logger


def log_event(logger: logging.Logger, event: str, **fields) -> None:
    logger.info(json.dumps({"event": event, **fields}, ensure_ascii=False, default=str))
