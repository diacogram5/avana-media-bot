-- 018_bank_health: source health tracking + Telegram file_id for bank audio.
-- Lets the bank really serve files: forwarded audio is stored by file_id
-- and re-sent without re-downloading. Health columns let the engine skip
-- dead sources and report per-source status.
ALTER TABLE bank_sources ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
ALTER TABLE bank_sources ADD COLUMN last_check INTEGER NOT NULL DEFAULT 0;
ALTER TABLE bank_sources ADD COLUMN last_error TEXT NOT NULL DEFAULT '';
ALTER TABLE bank_sources ADD COLUMN valid_count INTEGER NOT NULL DEFAULT 0;
ALTER TABLE audio_bank ADD COLUMN file_id TEXT NOT NULL DEFAULT '';
CREATE INDEX IF NOT EXISTS idx_audio_bank_title ON audio_bank(title);
CREATE INDEX IF NOT EXISTS idx_audio_bank_artist ON audio_bank(artist);
