-- Web-parity capabilities menu: converge existing DBs to the final
-- 12-feature list (titles, unique menu order, visibility). Fresh DBs get
-- the same list from DEFAULT_FEATURES in app/db.py (seeded after the
-- migrations), so this file only needs to fix rows that already exist.
-- visible=1 -> shown in the «قابلیت‌ها» menu and the persistent bottom
-- menu; link_fetch + audio_id keep their automatic trigger-on-send
-- behaviour too, tts + voice_to_text live behind the OpenAI key gate.
INSERT OR IGNORE INTO features (key, title, display_order, enabled, visible, message) VALUES
 ('suggest', 'درخواست آهنگ', 2, 1, 1, ''),
 ('downloader', 'دانلودر هوشمند', 3, 1, 1, ''),
 ('professional', ' 🎵 حرفه ای', 4, 1, 1, ''),
 ('lyrics', 'متن آهنگ', 5, 1, 1, ''),
 ('clear_chat', 'پاک‌سازی گفتگو', 6, 1, 1, ''),
 ('history', 'تاریخچه جستجو', 7, 1, 1, '');
UPDATE features SET title = 'جستجوی آهنگ و ویدیو', display_order = 1, visible = 1 WHERE key = 'song_search';
UPDATE features SET title = 'درخواست آهنگ', display_order = 2, visible = 1 WHERE key = 'suggest';
UPDATE features SET title = 'دانلودر هوشمند', display_order = 3, visible = 1 WHERE key = 'downloader';
UPDATE features SET title = ' 🎵 حرفه ای', display_order = 4, visible = 1 WHERE key = 'professional';
UPDATE features SET title = 'متن آهنگ', display_order = 5, visible = 1 WHERE key = 'lyrics';
UPDATE features SET title = 'پاک‌سازی گفتگو', display_order = 6, visible = 1 WHERE key = 'clear_chat';
UPDATE features SET title = 'تاریخچه جستجو', display_order = 7, visible = 1 WHERE key = 'history';
UPDATE features SET title = 'راهنما و پشتیبانی', display_order = 8, visible = 1 WHERE key = 'help_support';
UPDATE features SET title = 'شناسایی آهنگ از فایل', display_order = 9, visible = 1 WHERE key = 'audio_id';
UPDATE features SET title = 'تبدیل متن به ویس', display_order = 10, visible = 0 WHERE key = 'tts';
UPDATE features SET title = 'تبدیل ویس به متن', display_order = 11, visible = 0 WHERE key = 'voice_to_text';
UPDATE features SET title = 'دریافت از لینک', display_order = 12, visible = 1 WHERE key = 'link_fetch';
