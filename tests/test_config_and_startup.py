import logging

from app.config import Config, redact
from app.logging_config import get_logger


def test_no_token_is_detected_not_crash(monkeypatch):
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    cfg = Config(bot_token="")
    assert not cfg.has_token
    summary = cfg.safe_summary()
    assert "BOT_TOKEN" not in str(summary) or summary["token_preview"] in ("", "***")
    assert cfg.bot_token not in str(summary) or cfg.bot_token == ""


def test_redact_hides_secret():
    assert redact("123456:ABCDEF123456") not in ("123456:ABCDEF123456",)
    assert redact("123456:ABCDEF123456").startswith("123")
    assert redact("") == ""


def test_safe_summary_never_contains_token():
    cfg = Config(bot_token="999999:SECRET_TOKEN_VALUE")
    summary = cfg.safe_summary()
    assert "SECRET_TOKEN_VALUE" not in str(summary)


def test_logger_redacts_token_marker(caplog):
    logger = get_logger("test_redact_logger")
    with caplog.at_level(logging.INFO, logger="test_redact_logger"):
        logger.info("BOT_TOKEN=999999:SECRET_TOKEN_VALUE should not appear")
    assert "SECRET_TOKEN_VALUE" not in caplog.text
