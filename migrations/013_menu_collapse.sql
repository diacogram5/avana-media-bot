-- 013_menu_collapse: per-user persistent-menu collapse state.
--
-- collapsed=1 -> the bottom menu shows only «☰ منو»; collapsed=0 (default)
-- -> the full feature buttons plus a «🔽 بستن منو» toggle row.
--
-- HONEST PERSISTENCE NOTE: this table lives in bot.db. It survives bot code
-- restarts and redeploys on any persistent disk, but on Render's free tier
-- the filesystem is ephemeral: bot.db is wiped on every restart/redeploy,
-- so the preference resets to open there. That is a platform limit, not a
-- bug in this feature.
CREATE TABLE IF NOT EXISTS menu_collapse (
    telegram_id INTEGER PRIMARY KEY,
    collapsed INTEGER NOT NULL DEFAULT 0
);
