-- 004_audio_index: files the bot has actually seen (e.g. forwarded
-- channel audio) so a later identical / shortened upload can be matched
-- by audio content, not by name. No catalog claim is stored here: a match
-- is labelled as a channel/file match, never as a catalog identification.
CREATE TABLE IF NOT EXISTS audio_index (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    sha256 TEXT NOT NULL DEFAULT '',
    chunk_hashes TEXT NOT NULL DEFAULT '[]',
    size INTEGER NOT NULL DEFAULT 0,
    title TEXT NOT NULL DEFAULT '',
    artist TEXT NOT NULL DEFAULT '',
    channel TEXT NOT NULL DEFAULT '',
    post_url TEXT NOT NULL DEFAULT '',
    caption TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audio_index_sha ON audio_index(sha256);
