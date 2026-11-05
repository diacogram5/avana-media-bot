"""Fast Persian text search for the song bank (FTS5 + trigram).

User-provided design (2026-10-31), adapted to the codebase's sync sqlite3
model (conn passed as parameter, not aiosqlite global).

- Persian normalization: ي→ی, ك→ک, diacritics stripped, half-space → space
- FTS5 virtual table with trigram tokenizer for typo-tolerant search
- rapidfuzz token_set_ratio re-ranking for best matches first
- Channel registry for auto-indexing
- Deduplication via MD5(performer|title|duration//3)

This is the TEXT-METADATA layer of the bank. The existing audio
fingerprinting (app/features/bank.py, audio_bank.chroma) remains the
AUDIO-CONTENT layer. They complement each other.
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import time

# ---------------------------------------------------------------------------
# Persian text normalization
# ---------------------------------------------------------------------------

_MAP = {ord(a): b for a, b in {
    "ي": "ی", "ك": "ک", "ۀ": "ه", "أ": "ا",
    "إ": "ا", "ؤ": "و", "ئ": "ی", "\u200c": " ",
}.items()}
# Persian/Arabic digits → ASCII
_MAP.update({ord(c): str(i) for i, c in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_MAP.update({ord(c): str(i) for i, c in enumerate("٠١٢٣٤٥٦٧٨٩")})
_DIAC = re.compile(r"[\u064B-\u065F\u0670\u0640]")


def norm(s: str) -> str:
    """Normalize Persian text for search: lowercase, unify chars, strip diacritics."""
    s = (s or "").lower().translate(_MAP)
    s = _DIAC.sub("", s)
    s = re.sub(r"@\w+|https?://\S+", " ", s)
    s = re.sub(r"[^\w\s]|_", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def dedupe_key(performer: str, title: str, duration: int) -> str:
    """Stable dedup key: MD5(normalized performer|title|duration//3)."""
    return hashlib.md5(
        f"{norm(performer)}|{norm(title)}|{duration // 3}".encode()
    ).hexdigest()


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS bank_songs(
  id INTEGER PRIMARY KEY,
  doc_id INTEGER UNIQUE,
  dedupe TEXT UNIQUE,
  src_chat INTEGER,
  src_msg INTEGER,
  store_msg INTEGER,
  title TEXT,
  performer TEXT,
  duration INTEGER,
  size INTEGER,
  search TEXT,
  added INTEGER DEFAULT (strftime('%s','now')));
CREATE VIRTUAL TABLE IF NOT EXISTS bank_songs_fts USING fts5(
  search, content='bank_songs', content_rowid='id', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS bank_songs_ai AFTER INSERT ON bank_songs BEGIN
  INSERT INTO bank_songs_fts(rowid, search) VALUES (new.id, new.search); END;
CREATE TRIGGER IF NOT EXISTS bank_songs_ad AFTER DELETE ON bank_songs BEGIN
  INSERT INTO bank_songs_fts(bank_songs_fts, rowid, search)
  VALUES('delete', old.id, old.search); END;
CREATE TABLE IF NOT EXISTS bank_channels(
  username TEXT PRIMARY KEY,
  chat_id INTEGER,
  last_id INTEGER DEFAULT 0,
  active INTEGER DEFAULT 1,
  joined INTEGER DEFAULT 0);
"""


def ensure_schema(conn: sqlite3.Connection) -> None:
    """Create bank text-search tables if missing (idempotent)."""
    conn.executescript(SCHEMA)
    conn.commit()


# ---------------------------------------------------------------------------
# Songs
# ---------------------------------------------------------------------------

def seen(conn: sqlite3.Connection, doc_id: int, dkey: str) -> bool:
    """Check if a song (by doc_id or dedupe key) is already indexed."""
    row = conn.execute(
        "SELECT 1 FROM bank_songs WHERE doc_id=? OR dedupe=? LIMIT 1",
        (doc_id, dkey),
    ).fetchone()
    return row is not None


def add_song(conn: sqlite3.Connection, info: dict,
             src_chat: int, src_msg: int, store_msg: int) -> bool:
    """Index a song's metadata. Returns True if newly added."""
    ensure_schema(conn)
    search = norm(f"{info.get('performer', '')} {info.get('title', '')} {info.get('extra', '')}")
    dkey = dedupe_key(
        info.get("performer", ""), info.get("title", ""),
        int(info.get("duration", 0) or 0),
    )
    cur = conn.execute(
        "INSERT OR IGNORE INTO bank_songs"
        "(doc_id,dedupe,src_chat,src_msg,store_msg,title,performer,duration,size,search)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (info.get("doc_id"), dkey,
         src_chat, src_msg, store_msg,
         info.get("title", ""), info.get("performer", ""),
         int(info.get("duration", 0) or 0), int(info.get("size", 0) or 0),
         search),
    )
    conn.commit()
    return cur.rowcount > 0


def search(conn: sqlite3.Connection, q: str, limit: int = 8) -> list:
    """Fuzzy Persian text search. Returns up to `limit` rows, best first.

    Strategy: FTS5 trigram MATCH (AND first, then OR for typos),
    re-ranked by rapidfuzz token_set_ratio.
    """
    ensure_schema(conn)
    n = norm(q)
    toks = [t for t in n.split() if len(t) >= 3]
    rows = []
    if toks:
        for op in (" ", " OR "):  # AND first, then OR for typos
            match = op.join(f'"{t}"' for t in toks)
            try:
                cur = conn.execute(
                    "SELECT s.* FROM bank_songs_fts f "
                    "JOIN bank_songs s ON s.id=f.rowid "
                    "WHERE bank_songs_fts MATCH ? "
                    "ORDER BY bm25(bank_songs_fts) LIMIT ?",
                    (match, limit * 4),
                )
                rows = cur.fetchall()
            except Exception:
                rows = []
            if rows:
                break
    else:
        cur = conn.execute(
            "SELECT * FROM bank_songs WHERE search LIKE ? LIMIT ?",
            (f"%{n}%", limit * 4),
        )
        rows = cur.fetchall()

    if not rows:
        return []

    # Re-rank with rapidfuzz (best fuzzy match first)
    try:
        from rapidfuzz import fuzz as _fuzz
        rows = sorted(
            rows,
            key=lambda r: _fuzz.token_set_ratio(n, r["search"] or ""),
            reverse=True,
        )
    except ImportError:
        pass  # bm25 order is already good
    return rows[:limit]


def count_songs(conn: sqlite3.Connection) -> int:
    """Number of indexed songs (for the 'bank has N files' message)."""
    ensure_schema(conn)
    row = conn.execute("SELECT COUNT(*) FROM bank_songs").fetchone()
    return row[0] if row else 0


# ---------------------------------------------------------------------------
# Channels
# ---------------------------------------------------------------------------

def add_channel(conn: sqlite3.Connection, username: str) -> None:
    ensure_schema(conn)
    conn.execute(
        "INSERT OR IGNORE INTO bank_channels(username) VALUES(?)",
        (username.lstrip("@"),),
    )
    conn.commit()


def to_join(conn: sqlite3.Connection) -> list:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM bank_channels WHERE active=1 AND joined=0"
    ).fetchall()


def joined(conn: sqlite3.Connection) -> list:
    ensure_schema(conn)
    return conn.execute(
        "SELECT * FROM bank_channels WHERE active=1 AND joined=1"
    ).fetchall()


def mark_joined(conn: sqlite3.Connection, username: str, chat_id: int) -> None:
    ensure_schema(conn)
    conn.execute(
        "UPDATE bank_channels SET joined=1, chat_id=? WHERE username=?",
        (chat_id, username),
    )
    conn.commit()


def set_last(conn: sqlite3.Connection, username: str, last_id: int) -> None:
    ensure_schema(conn)
    conn.execute(
        "UPDATE bank_channels SET last_id=? WHERE username=?",
        (last_id, username),
    )
    conn.commit()


def deactivate(conn: sqlite3.Connection, username: str) -> None:
    ensure_schema(conn)
    conn.execute(
        "UPDATE bank_channels SET active=0 WHERE username=?",
        (username,),
    )
    conn.commit()
