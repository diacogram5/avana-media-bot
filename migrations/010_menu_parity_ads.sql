-- 010: web-parity menu (10 visible buttons) + audience ad counters.
-- link_fetch + audio_id become visible buttons alongside their automatic
-- trigger-on-send behaviour (exact web parity: 10 menu items).
UPDATE features SET visible = 1 WHERE key IN ('link_fetch', 'audio_id');
-- Per-user bot-answer counter for the integrated audience-ad schedule.
CREATE TABLE IF NOT EXISTS ad_counters (
    user_id INTEGER PRIMARY KEY,
    count INTEGER NOT NULL DEFAULT 0
);
