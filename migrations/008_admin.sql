-- 008_admin: admin extension (adm: namespace).
-- Outbox of outgoing bot messages so "clear chat" can delete them best-effort.
-- Ads table gains title/photo/link/button/stats columns (old rows keep working).
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_at INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_outbox_user ON outbox (user_id, created_at);

ALTER TABLE ads ADD COLUMN title TEXT NOT NULL DEFAULT '';
ALTER TABLE ads ADD COLUMN photo_file_id TEXT NOT NULL DEFAULT '';
ALTER TABLE ads ADD COLUMN link TEXT NOT NULL DEFAULT '';
ALTER TABLE ads ADD COLUMN button_text TEXT NOT NULL DEFAULT '';
ALTER TABLE ads ADD COLUMN views INTEGER NOT NULL DEFAULT 0;
ALTER TABLE ads ADD COLUMN clicks INTEGER NOT NULL DEFAULT 0;
