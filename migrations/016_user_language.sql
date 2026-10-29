-- 016_user_language: per-user UI language preference (fa/en/ar).
--
-- lang is NULL until the user picks a language on first /start; a NULL
-- row means "never chose" (the picker must be shown) and any consumer
-- falls back to "fa" (Persian). Follows the exact pattern of
-- 013_menu_collapse.sql (menu_collapse table + db helpers).
--
-- HONEST PERSISTENCE NOTE: this table lives in bot.db. It survives bot code
-- restarts and redeploys on any persistent disk, but on Render's free tier
-- the filesystem is ephemeral: bot.db is wiped on every restart/redeploy,
-- so the language choice resets to the default (fa) there. That is a
-- platform limit, not a bug in this feature. The choice never affects
-- download/identify logic — it only changes UI chrome strings.
CREATE TABLE IF NOT EXISTS user_language (
    telegram_id INTEGER PRIMARY KEY,
    lang TEXT NOT NULL DEFAULT 'fa'
);
