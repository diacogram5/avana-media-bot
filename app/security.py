"""Security helpers: link validation / SSRF protection, rate limiting,
password hashing, secret-safe logging. No token or password is ever logged.
"""
from __future__ import annotations

import hashlib
import hmac
import ipaddress
import re
import secrets
import socket
import time
from urllib.parse import urlparse

ALLOWED_DOMAINS = {
    "youtube.com", "www.youtube.com", "music.youtube.com",
    "youtu.be",
    "instagram.com", "www.instagram.com",
    "aparat.com", "www.aparat.com",
    "tiktok.com", "www.tiktok.com", "vm.tiktok.com",
}
# Short-link hosts we are willing to resolve (HEAD with redirects) before
# re-validating the final URL against the rules above.
SHORTENER_HOSTS = {
    "bit.ly", "tinyurl.com", "t.co", "is.gd", "cutt.ly", "goo.gl",
    "shorturl.at", "rebrand.ly",
}
DIRECT_FILE_EXTENSIONS = {".mp3", ".m4a", ".ogg", ".wav", ".mp4", ".webm", ".mov", ".mkv"}

PRIVATE_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}
SSRF_BLOCKED_SUFFIXES = (".local", ".internal", ".lan")


def _is_private_ip(host: str) -> bool:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return False
    return bool(
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def domain_allowed(host: str) -> bool:
    host = host.lower().strip()
    if host in ALLOWED_DOMAINS:
        return True
    # Direct file URLs are allowed only from any public host, but the
    # SSRF check below still applies; allowlist check is done in validate_link.
    return False


def is_direct_file_url(path: str) -> bool:
    return any(path.lower().split("?")[0].endswith(ext) for ext in DIRECT_FILE_EXTENSIONS)


class LinkValidation:
    def __init__(self, ok: bool, reason: str = "", host: str = "", kind: str = ""):
        self.ok = ok
        self.reason = reason
        self.host = host
        self.kind = kind

    def __bool__(self) -> bool:
        return self.ok


def validate_link(url: str, resolve_dns: bool = False) -> LinkValidation:
    """Validate a user-supplied media link. Never fetch private content.

    Rules: http/https only, no userinfo trickery, no private/loopback/
    link-local/reserved IP, no .local/.internal hostnames, host must be on
    the allowlist OR a direct audio/video file URL on a public host.
    ``resolve_dns=True`` additionally rejects hostnames that resolve to a
    private IP (use it before any real fetch; it needs network/DNS).
    """
    if not url or len(url) > 2048:
        return LinkValidation(False, "empty_or_too_long")
    url = url.strip()
    try:
        parsed = urlparse(url)
    except ValueError:
        return LinkValidation(False, "unparsable")
    if parsed.scheme not in ("http", "https"):
        return LinkValidation(False, "scheme_not_allowed")
    if parsed.username or parsed.password:
        return LinkValidation(False, "userinfo_not_allowed")
    host = (parsed.hostname or "").lower()
    if not host:
        return LinkValidation(False, "no_host")
    if host in PRIVATE_HOSTNAMES or host.endswith(SSRF_BLOCKED_SUFFIXES):
        return LinkValidation(False, "private_host_blocked", host)
    if _is_private_ip(host):
        return LinkValidation(False, "private_ip_blocked", host)
    if resolve_dns:
        try:
            for _family, _type, _proto, _canon, sockaddr in socket.getaddrinfo(host, None):
                if _is_private_ip(sockaddr[0]):
                    return LinkValidation(False, "resolves_to_private_ip", host)
        except socket.gaierror:
            return LinkValidation(False, "dns_failed", host)
    if host in ALLOWED_DOMAINS:
        kind = "youtube" if "youtube" in host or host == "youtu.be" else (
            "instagram" if "instagram" in host else "aparat" if "aparat" in host
            else "tiktok" if "tiktok" in host else "video")
        return LinkValidation(True, "", host, kind)
    if is_direct_file_url(parsed.path):
        return LinkValidation(True, "", host, "direct_file")
    return LinkValidation(False, "domain_not_allowed", host)


def is_valid_telegram_id(value: str) -> bool:
    return bool(re.fullmatch(r"\d{5,20}", value.strip()))


def resolve_short_link(url: str, timeout: float = 8.0) -> str:
    """Resolve a known short-link host to its final URL (no fetch of body).

    Returns the final URL, or the original URL when the host is not a known
    shortener or resolution fails. The caller must re-validate the result
    with :func:`validate_link` before any use.
    """
    try:
        host = (urlparse(url.strip()).hostname or "").lower()
    except ValueError:
        return url
    if host not in SHORTENER_HOSTS:
        return url
    try:
        import httpx
        with httpx.Client(timeout=timeout, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as client:
            resp = client.head(url, follow_redirects=True)
        final = str(resp.url)
        return final if final and final != url else url
    except Exception:
        return url


def sanitize_text(value: str, max_len: int = 1000) -> str:
    value = (value or "").strip()
    return value[:max_len]


class RateLimiter:
    """In-memory rate limiter with the same interface a Redis one would use.

    ``allow(key, limit, window_seconds)`` returns True on the first
    ``limit`` calls inside the window, False afterwards. This is the stage-1
    implementation; swapping the backend only means swapping this class.
    """

    def __init__(self) -> None:
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        now = time.time()
        hits = [t for t in self._hits.get(key, []) if now - t < window_seconds]
        if len(hits) >= limit:
            self._hits[key] = hits
            return False
        hits.append(now)
        self._hits[key] = hits
        return True

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)


# --- passwords (web panel is deferred; this is ready for it) --------------
def hash_password(password: str, salt: bytes | None = None) -> str:
    """PBKDF2-HMAC-SHA256, stdlib only. Format: pbkdf2$<salt-hex>$<hash-hex>."""
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 200_000)
    return f"pbkdf2${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        _name, salt_hex, hash_hex = stored.split("$")
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), 200_000
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except Exception:
        return False
