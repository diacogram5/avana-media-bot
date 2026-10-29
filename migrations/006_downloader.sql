-- 006_downloader: smart-downloader job queue (Telegram-native dl: module).
-- One row per download request; the live worker edits the job message in
-- place while the job moves through queued -> working -> ready/failed.
CREATE TABLE IF NOT EXISTS download_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL DEFAULT 0,
    url TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'unknown',
    content_type TEXT NOT NULL DEFAULT 'unknown',
    status TEXT NOT NULL DEFAULT 'queued',
    stage TEXT NOT NULL DEFAULT 'queued',
    progress INTEGER NOT NULL DEFAULT 0,
    formats_json TEXT NOT NULL DEFAULT '[]',
    chosen_format TEXT NOT NULL DEFAULT '',
    result_file_id TEXT NOT NULL DEFAULT '',
    result_url TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_download_jobs_user ON download_jobs(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_download_jobs_status ON download_jobs(status);
CREATE INDEX IF NOT EXISTS idx_download_jobs_url ON download_jobs(url);
