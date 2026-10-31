-- File cache for Telegram file_id reuse (Requirement #3)
-- Stores Telegram file_ids for previously sent files to avoid re-upload.
-- Also tracks cache metadata for management (Requirement #4).

CREATE TABLE IF NOT EXISTS file_cache (
    cache_key TEXT PRIMARY KEY,  -- e.g., "song:artist|title" or "hash:abc123"
    file_id TEXT NOT NULL,       -- Telegram file_id for reuse
    file_type TEXT NOT NULL,     -- 'audio', 'video', 'document'
    title TEXT,                  -- Song title (for reference)
    artist TEXT,                 -- Artist name (for reference)
    file_size INTEGER,           -- Size in bytes (for validation)
    duration_sec REAL,           -- Duration (for validation)
    source TEXT,                 -- Where the file came from
    created_at INTEGER NOT NULL, -- Unix timestamp
    expires_at INTEGER NOT NULL, -- Unix timestamp (TTL)
    use_count INTEGER DEFAULT 1, -- How many times reused
    last_used_at INTEGER,        -- Last reuse timestamp
    is_valid INTEGER DEFAULT 1   -- 0 = invalid/corrupt, 1 = valid
);

CREATE INDEX IF NOT EXISTS idx_file_cache_expires ON file_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_file_cache_valid ON file_cache(is_valid);

-- Reported/blocked content (Requirement #4)
-- Content that has been reported and should not be served.

CREATE TABLE IF NOT EXISTS reported_content (
    cache_key TEXT PRIMARY KEY,
    reason TEXT,
    reported_at INTEGER NOT NULL,
    reported_by INTEGER,  -- Telegram user ID of reporter
    status TEXT DEFAULT 'pending'  -- 'pending', 'blocked', 'dismissed'
);

-- Speed measurements (Requirement #7)
-- Tracks processing time with/without cache for comparison.

CREATE TABLE IF NOT EXISTS speed_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    operation TEXT NOT NULL,  -- 'identify', 'download', 'send'
    cache_hit INTEGER NOT NULL, -- 0 = miss, 1 = hit
    duration_ms INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_speed_metrics_op ON speed_metrics(operation, cache_hit);
