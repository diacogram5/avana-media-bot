-- 014_menu_titles: fix the seeded «professional» title's spacing bugs
-- (leading space + «حرفه ای» instead of «حرفه‌ای»).
-- Only rows the admin never customized are touched: any title that does
-- not exactly match a known seeded default is left alone.
UPDATE features SET title='🎵 حرفه‌ای'
WHERE key='professional'
  AND (title=' 🎵 حرفه ای' OR title='🎵 حرفه ای' OR title='🎵 حرفه‌ای ');
