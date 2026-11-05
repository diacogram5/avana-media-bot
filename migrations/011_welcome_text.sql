-- 011_welcome_text: replace the stale seeded welcome with the exact Avano
-- welcome text. Only rows the admin never customized (still the old seed
-- value) are touched; a custom welcome_message is left alone.
UPDATE settings SET value='🎵 به آوانو خوش آمدی!

دستیار هوشمند جستجو و شناسایی آهنگ و ویدیو.

🔎 جستجوی آهنگ و خواننده
🎧 شناسایی موسیقی از روی کلیپ
📹 دریافت ویدیو از منابع پشتیبانی‌شده
🎼 جستجوی آهنگ‌های مشابه و دریافت فایل موسیقی

👇 برای شروع، یکی از گزینه‌های منو را انتخاب کن.'
WHERE key='welcome_message' AND value='سلام! به ربات جستجوی آهنگ و ویدیو خوش آمدید.';
