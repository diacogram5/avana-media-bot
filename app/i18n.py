"""Minimal i18n: Persian (fa) is the default and complete language.
English (en) exists only so the structure is ready for other languages;
missing keys fall back to Persian. All user-facing strings live here or in
DB settings (which admins can edit from Telegram), never scattered in code.
"""
from __future__ import annotations

FA = {
    "welcome": '🎵 به آوانو خوش آمدی!\n\nدستیار هوشمند جستجو و شناسایی آهنگ و ویدیو.\n\n🔎 جستجوی آهنگ و خواننده\n🎧 شناسایی موسیقی از روی کلیپ\n📹 دریافت ویدیو از منابع پشتیبانی‌شده\n🎼 جستجوی آهنگ‌های مشابه و دریافت فایل موسیقی\n\n👇 برای شروع، یکی از گزینه‌های منو را انتخاب کن.',
        "voice_usage": "🎙 برای ویس فارسی بنویسید:\n/voice زن <متن>\n/voice مرد <متن>",
    "audio_identifying": "🎧 در حال شناسایی آهنگ از روی صدا…",
    "audio_needs_token": "⚠️ شناسایی صدا هنوز فعال نیست: توکن سرویس شناسایی صدا تنظیم نشده است. آهنگ را با نام، خواننده یا خط ترانه جستجو کنید؛ نتیجه نامربوط به‌عنوان آهنگ شناسایی‌شده نشان داده نمی‌شود.",
    "audio_not_found": "😕 آهنگ این صدا در پایگاه شناسایی پیدا نشد. نام یا خط ترانه را بفرستید.",
    "audio_error": "⚠️ شناسایی صدا با خطا مواجه شد. دوباره تلاش کنید.",
    "audio_identified": "✅ آهنگ شناسایی شد:",
    "song_not_identified": "آهنگ این ویدیو شناسایی نشد.",
    "send_video_for_identification": "🎬 برای شناسایی آهنگ، ویدیو را مستقیم بفرستید.",
    "catalog_preview_unavailable": "پیش‌نمایش و لینک رسمی این آهنگ در کاتالوگ پیدا نشد.",
    "tts_needs_key": "⚠️ تبدیل متن به ویس هنوز فعال نیست: کلید موتور گفتار (OPENAI_API_KEY یا کلید Azure) تنظیم نشده است. صدای نامربوط به‌جای صدای فارسی پخش نمی‌شود.",
    "tts_error": "⚠️ ساخت ویس با خطا مواجه شد. دوباره تلاش کنید.",
    "tts_ready": "🎙 ویس فارسی آماده شد:",
    "menu_voice_to_text": "🎙 تبدیل ویس به متن",
    "voice_to_text_usage": "🎙 برای تبدیل ویس به متن، دکمه «تبدیل ویس به متن» را بزنید یا /voicetotext را بفرستید، سپس یک ویس بفرستید.",
    "voice_to_text_prompt": "🎙 حالا یک ویس بفرستید تا به متن تبدیل شود.",
    "voice_to_text_transcribing": "⏳ در حال تبدیل ویس به متن…",
    "voice_to_text_needs_key": "⚠️ تبدیل ویس به متن هنوز فعال نیست: کلید موتور شنیداری (OPENAI_API_KEY یا کلید Azure) تنظیم نشده است. متن جعلی ساخته نمی‌شود.",
    "voice_to_text_error": "⚠️ تبدیل ویس به متن با خطا مواجه شد. دوباره تلاش کنید.",
    "voice_to_text_empty": "😕 در این ویس متنی تشخیص داده نشد.",
    "voice_to_text_result": "📝 متن ویس:",
    "link_audio_note": "ℹ️ شناسایی آهنگِ لینک، به سرویس شناسایی صدا نیاز دارد؛ بدون آن، فقط لینک رسمی بالا معتبر است و نتیجه نامربوط نشان داده نمی‌شود.",
"help": (
        "📖 راهنما\n\n"
        "• /start — شروع و منوی اصلی\n"
        "• /help — همین راهنما\n"
        "• /cancel — لغو عملیات جاری\n\n"
        "می‌توانید نام آهنگ یا ویدیو را مستقیم بفرستید، یا یک لینک معتبر ارسال کنید."
    ),
    "about": "ℹ️ درباره ربات\n\nربات جستجوی آهنگ و ویدیو؛ فارسی، ساده و ماژولار.\nجستجو از منابع رسمی و مجاز انجام می‌شود و لینک اصلی منبع همیشه نمایش داده می‌شود.",
    "cancelled": "✅ عملیات جاری لغو شد. برای شروع دوباره /start را بزنید.",
    "nothing_to_cancel": "در حال حاضر عملیاتی در جریان نیست.",
    "menu_song": "🎵 جستجوی آهنگ",
    "menu_artist": "🎤 جستجوی خواننده",
    "menu_music_video": "🎬 جستجوی موزیک‌ویدیو",
    "menu_video": "📹 جستجوی ویدیو",
    "menu_link": "🔗 دریافت از لینک",
    "menu_recent": "🕘 جستجوهای اخیر",
    "menu_settings": "⚙️ تنظیمات",
    "menu_help": "❓ راهنما و پشتیبانی",
    "menu_admin": "🛡 پنل مدیریت",
    "ask_query": "🔍 عبارت جستجو را بفرستید (نام آهنگ، خواننده یا ویدیو):",
    "ask_link": "🔗 لینک رسانه را بفرستید:",
    "searching": "⏳ در حال جستجو…",
    "no_results": "😕 نتیجه‌ای پیدا نشد. عبارت دیگری را امتحان کنید.",
    "search_failed": "⚠️ جستجو با خطا مواجه شد. لطفاً دوباره تلاش کنید.",
    "feature_disabled": "⛔ این بخش در حال حاضر غیرفعال است.",
    "not_admin": "⛔ دسترسی مدیریت ندارید.",
    "blocked": "⛔ حساب شما مسدود شده است. برای پشتیبانی پیام بدهید.",
    "daily_limit": "⏳ سقف جستجوی امروز شما تمام شده است. فردا دوباره تلاش کنید.",
    "channel_required": "🔒 برای استفاده از این بخش، ابتدا در کانال‌های زیر عضو شوید، سپس دوباره تلاش کنید:",
    "link_invalid": "❌ لینک معتبر نیست یا دامنه آن پشتیبانی نمی‌شود.",
    "link_blocked": "⛔ این لینک به دلایل امنیتی مسدود شد (آدرس داخلی یا مشکوک).",
    "link_official": "🔗 لینک رسمی منبع:",
    "link_not_downloadable": "ℹ️ دانلود مستقیم این منبع ممکن نیست؛ لینک رسمی بالا را باز کنید.",
    "settings_title": "⚙️ تنظیمات شما",
    "settings_saved": "✅ تنظیمات ذخیره شد.",
    "admin_title": "🛡 پنل مدیریت",
    "admin_users": "👥 کاربران",
    "admin_features": "🧩 قابلیت‌ها",
    "admin_settings": "⚙️ تنظیمات ربات",
    "admin_audit": "📋 گزارش اقدامات",
    "back": "⬅️ بازگشت",
    "unknown_command": "دستور ناشناخته است. /help را ببینید.",
    # Brief errors (one line, no jargon) + new professional features.
    "err_search": "⚠️ جستجو با خطا مواجه شد. دوباره تلاش کنید.",
    "err_identify": "⚠️ شناسایی آهنگ با خطا مواجه شد. ویدیو را مستقیم بفرستید.",
    "err_file": "⚠️ ارسال فایل با خطا مواجه شد. دوباره تلاش کنید.",
    "err_lyrics": "⚠️ متن آهنگ با خطا مواجه شد. دوباره تلاش کنید.",
    "err_voice": "⚠️ ساخت ویس با خطا مواجه شد. دوباره تلاش کنید.",
    "err_translate": "⚠️ ترجمه با خطا مواجه شد. دوباره تلاش کنید.",
    "err_suggest": "⚠️ پیشنهاد آهنگ با خطا مواجه شد. دوباره تلاش کنید.",
    "menu_request_song": "🎵 درخواست آهنگ",
    "menu_playlists": "🎶 پلی‌لیست‌های من",
    "menu_favorites": "❤️ علاقه‌مندی‌ها",
    "menu_human_voice_web": "🎙 تبدیل گفتار انسانی در وب",
    "suggest_prompt": "🎵 حال‌وهوای آهنگ را بنویسید؛ مثلاً: یه آهنگ غمگین برای شب می‌خوام، یا آهنگ سیستم بیس‌دار.",
    "suggest_none": "😕 برای این حال‌وهوا آهنگ مناسبی پیدا نشد. جمله دیگری را امتحان کنید.",
    "suggest_result": "🎵 پیشنهاد هوشمند برای شما:",
    "playlists_empty": "😕 هنوز پلی‌لیستی نساخته‌اید. با /newplaylist <نام> یکی بسازید؛ مثلاً: /newplaylist غمگین",
    "playlist_created": "✅ پلی‌لیست ساخته شد.",
    "playlist_added": "✅ آهنگ به پلی‌لیست اضافه شد.",
    "playlist_exists": "ℹ️ این آهنگ قبلاً در پلی‌لیست است.",
    "lyrics_not_found": "😕 متن دقیق این آهنگ پیدا نشد. متن نزدیک یا اشتباه فرستاده نمی‌شود.",
    "lyrics_translate_prompt": "🌐 زبان مقصد ترجمه را انتخاب کنید:",
    "lyrics_copy_label": "📋 نسخه قابل کپی و اشتراک:",
    "channel_post": "📡 پست چنل تلگرام (نه آهنگ کاتالوگ):",
    "channel_not_configured": "📡 چنل‌های تلگرام تنظیم نشده است؛ برای جستجوی چنل‌ها، مالک باید CHANNEL_USERNAMES را در تنظیمات وارد کند.",
    "channel_no_result": "📡 چنل‌های تلگرام: نتیجه‌ای نداشت.",
    "link_stage_not_found": "آهنگ این ویدیو شناسایی نشد. اثر صوتی: در کاتالوگ پیدا نشد؛ چنل‌های تلگرام: نتیجه‌ای نداشت.",
    "audio_index_match": "📡 این صدا در فایل‌های ذخیره‌شده چنل پیدا شد (نه آهنگ کاتالوگ):",
    "ai_channel_note": "ℹ️ این آهنگ شناسایی نشد؛ ممکن است ساخته هوش مصنوعی یا فقط در چنل‌ها باشد. فایل خودتان برای دانلود برگشت.",
    "similar_songs": "🔍 جستجوی آهنگ‌های مشابه",
}

EN = {
    "welcome": "Welcome! Send a song, artist or video name, or use the menu.",
    "help": "Commands: /start /search /settings /about /cancel",
    "about": "Music and video search bot.",
    "cancelled": "Operation cancelled.",
    "nothing_to_cancel": "No operation in progress.",
    "feature_disabled": "This feature is currently disabled.",
    "not_admin": "You do not have admin access.",
    "blocked": "Your account is blocked.",
    "daily_limit": "Your daily search limit is reached.",
    "channel_required": "Please join the required channels first.",
    "link_invalid": "Invalid or unsupported link.",
    "link_blocked": "This link was blocked for security reasons.",
    "link_official": "Official source link:",
}

TRANSLATIONS = {"fa": FA, "en": EN}


def t(key: str, lang: str = "fa") -> str:
    table = TRANSLATIONS.get(lang, FA)
    return table.get(key) or FA.get(key, key)


def available_languages() -> list[str]:
    return list(TRANSLATIONS)
