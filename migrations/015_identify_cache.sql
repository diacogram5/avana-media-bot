-- Audio-content-hash identify cache: sha256 of the input audio bytes
-- (or a stable source-URL key) -> terminal identify result.
-- Lets a re-identify of the same audio skip the download AND the
-- Shazam/AudD lookups. Only terminal results (identified / not_found)
-- are stored; lookup errors are never cached.
CREATE TABLE IF NOT EXISTS identify_cache (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    artist TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL
);
