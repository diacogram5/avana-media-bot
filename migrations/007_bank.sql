-- 007_bank: superior-AI bank for two-stage song identification.
-- Samples are kept in bank_samples (landmark fingerprints of the user's
-- audio sample), indexed files in audio_bank (channel + superior-AI site
-- files only), sources in bank_sources, per-channel index state in
-- bank_channel_status. No audience message ever carries a channel name.
CREATE TABLE IF NOT EXISTS bank_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'channel',
    identifier TEXT NOT NULL DEFAULT '',
    label TEXT NOT NULL DEFAULT '',
    enabled INTEGER NOT NULL DEFAULT 1,
    sort_order INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS audio_bank (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL DEFAULT 'channel',
    channel TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    artist TEXT NOT NULL DEFAULT '',
    chroma TEXT NOT NULL DEFAULT '',
    file_url TEXT NOT NULL DEFAULT '',
    post_url TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bank_samples (
    token TEXT UNIQUE,
    kind TEXT NOT NULL DEFAULT '',
    chroma TEXT NOT NULL DEFAULT '',
    clue TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'ready',
    result_json TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS bank_channel_status (
    channel TEXT UNIQUE,
    status TEXT NOT NULL DEFAULT 'unchecked',
    seen_posts INTEGER NOT NULL DEFAULT 0,
    audio_files INTEGER NOT NULL DEFAULT 0,
    indexed_count INTEGER NOT NULL DEFAULT 0,
    has_fingerprint INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_audio_bank_source ON audio_bank(source);
CREATE INDEX IF NOT EXISTS idx_audio_bank_post ON audio_bank(post_url);
CREATE INDEX IF NOT EXISTS idx_bank_sources_kind ON bank_sources(kind);
