"""Configuration: environment variables only. Never hard-code a token or key.

Load order: real environment wins over a local ``.env`` file (if present).
``.env`` itself is never committed (see .gitignore) and must never contain
a real secret in any example or document.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv() -> None:
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv()


def redact(value: str | None) -> str:
    """Return a log-safe version of a secret: first/last chars only, or ''."""
    if not value:
        return ""
    if len(value) <= 8:
        return "***"
    return f"{value[:3]}***{value[-3:]}"


@dataclass
class Config:
    bot_token: str = field(default_factory=lambda: os.environ.get("BOT_TOKEN", ""))
    owner_id: int | None = None
    db_path: str = field(default_factory=lambda: os.environ.get("DB_PATH", str(ROOT / "data" / "bot.db")))
    language: str = field(default_factory=lambda: os.environ.get("BOT_LANGUAGE", "fa"))
    max_file_mb: int = 50
    max_concurrent_downloads: int = 2
    daily_search_limit: int = 50
    # Core-feature service keys — env only, never logged in full.
    # Primary speech engine (both directions): OpenAI (Whisper + TTS).
    openai_api_key: str = field(default_factory=lambda: os.environ.get("OPENAI_API_KEY", ""))
    audd_token: str = field(default_factory=lambda: os.environ.get("AUDD_API_TOKEN", ""))
    # Jamendo (free client_id at developer.jamendo.com): CC-music catalog
    # with real download URLs.
    jamendo_client_id: str = field(default_factory=lambda: os.environ.get("JAMENDO_CLIENT_ID", ""))
    # Spotify Web API (free at developer.spotify.com): metadata, official
    # links, 30s previews. No full downloads (by design).
    spotify_client_id: str = field(default_factory=lambda: os.environ.get("SPOTIFY_CLIENT_ID", ""))
    spotify_client_secret: str = field(default_factory=lambda: os.environ.get("SPOTIFY_CLIENT_SECRET", ""))
    azure_speech_key: str = field(default_factory=lambda: os.environ.get("AZURE_SPEECH_KEY", ""))
    azure_speech_region: str = field(default_factory=lambda: os.environ.get("AZURE_SPEECH_REGION", ""))
    web_admin_enabled: bool = field(default_factory=lambda: os.environ.get("WEB_ADMIN_ENABLED", "1") == "1")
    web_admin_port: int = field(default_factory=lambda: int(os.environ.get("WEB_ADMIN_PORT", "8080") or 8080))
    web_admin_token: str = field(default_factory=lambda: os.environ.get("WEB_ADMIN_TOKEN", ""))
    # Web fallback (existing web version only, never fabricated): empty by
    # default, button hidden when empty. Set via env WEB_URL or DB setting.
    web_url: str = field(default_factory=lambda: os.environ.get("WEB_URL", ""))
    # Bot ID/link shown only on shared files/text, never in the bot chat.
    bot_id: str = field(default_factory=lambda: os.environ.get("BOT_ID", ""))
    # Public Telegram channels for the second (channel) engine, comma list.
    channel_usernames: str = field(default_factory=lambda: os.environ.get("CHANNEL_USERNAMES", ""))

    def __post_init__(self) -> None:
        raw_owner = os.environ.get("OWNER_ID", "")
        if raw_owner.strip().isdigit():
            self.owner_id = int(raw_owner)
        try:
            self.max_file_mb = int(os.environ.get("MAX_FILE_MB", "50"))
        except ValueError:
            self.max_file_mb = 50
        try:
            self.daily_search_limit = int(os.environ.get("DAILY_SEARCH_LIMIT", "50"))
        except ValueError:
            self.daily_search_limit = 50

    @property
    def has_token(self) -> bool:
        return bool(self.bot_token)

    def safe_summary(self) -> dict:
        """Summary safe to log: never includes a token or key value."""
        return {
            "has_token": self.has_token,
            "token_preview": redact(self.bot_token),
            "owner_id": self.owner_id,
            "db_path": self.db_path,
            "language": self.language,
            "audd_configured": bool(self.audd_token),
            "jamendo_configured": bool(self.jamendo_client_id),
            "spotify_configured": bool(self.spotify_client_id and self.spotify_client_secret),
            "openai_speech_configured": bool(self.openai_api_key),
            "azure_tts_configured": bool(self.azure_speech_key and self.azure_speech_region),
            "web_admin_enabled": self.web_admin_enabled,
            "web_url_set": bool(self.web_url),
            "bot_id_set": bool(self.bot_id),
            "channels_configured": bool(self.channel_usernames),
        }


def get_config() -> Config:
    return Config()
