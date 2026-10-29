-- 012_clear_chat_titles: rename the clear-chat feature and the wipe card to
-- the two explicitly distinct operation titles. Only rows the admin never
-- customized are touched; a custom title is left alone.
UPDATE features SET title='پاک‌سازی وضعیت گفت‌وگو'
WHERE key='clear_chat' AND title='پاک‌سازی گفتگو';
