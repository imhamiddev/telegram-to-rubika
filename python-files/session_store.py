"""
session_store.py — ذخیره‌سازی سبک متادیتای session‌های فعال روی دیسک

چرا این فایل لازمه؟
همه‌ی session‌های فعال (`_sessions`, `_link_sessions`, ...) در telegram_bot.py
به‌صورت دیکشنری‌های in-memory نگه داشته می‌شن. اگه ربات کرش کنه یا سرور
ری‌استارت بشه، همه‌ی این اطلاعات از دست می‌رن — حتی اگه خود فایل‌های
دانلودشده هنوز سالم روی دیسک باشن. کاربر باید همه‌چیز رو از اول بفرسته.

این ماژول فقط «متادیتای قابل‌ذخیره» (مسیر فایل‌ها، اسم‌ها، وضعیت ارسال،
شمارنده‌ی retry) رو در یک فایل JSON نگه می‌داره — نه خود آبجکت‌های تلگرام
(مثل Message) که اصلاً قابل serialize نیستن. با هر تغییر در یک session،
telegram_bot.py این فایل رو به‌روزرسانی می‌کنه؛ در استارت‌آپ بعدی، اگه
فایل‌های دانلودشده هنوز روی دیسک باشن، session بازیابی و به کاربر اطلاع
داده می‌شه که می‌تونه ادامه بده؛ در غیر این صورت پاک می‌شه.

⚠️ این یک persistence کامل و تراکنشی نیست (مثل یک دیتابیس واقعی)؛ صرفاً
best-effort است — هدف جلوگیری از گم‌شدن کامل کار کاربر در ری‌استارت‌های
معمولی است، نه تضمین صددرصدی در برابر هر نوع crash.
"""

import os
import json
import logging

logger = logging.getLogger(__name__)

from config import BASE_DIR

SESSIONS_FILE = os.path.join(BASE_DIR, "active_sessions.json")

# کلیدهایی از یک session که واقعاً قابل serialize و بازیابی هستن.
# مواردی مثل "pending_forwards" (لیستی از آبجکت Message تلگرام) و
# "collecting_forwards" عمداً کنار گذاشته می‌شن چون یا قابل ذخیره نیستن یا
# فقط برای یک پنجره‌ی چند ثانیه‌ای (FORWARD_COLLECT_SECONDS) معتبرن که با
# ری‌استارت ربات دیگه معنی نداره.
_PERSISTABLE_KEYS = {
    "files", "waiting", "safe_mode", "password", "caption",
    "start_from", "retries", "part_files",
}


def _serialize_session(session: dict) -> dict:
    return {k: v for k, v in session.items() if k in _PERSISTABLE_KEYS}


def load_all() -> dict:
    """
    محتوای فایل ذخیره‌شده رو می‌خونه. اگه فایل نبود یا خراب بود (مثلاً به‌خاطر
    قطع برق وسط نوشتن)، یک دیکشنری خالی برمی‌گردونه تا ربات به‌جای کرش کردن
    در استارت‌آپ، صرفاً بدون session‌های قدیمی شروع بشه.
    """
    if not os.path.exists(SESSIONS_FILE):
        return {}
    try:
        with open(SESSIONS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        logger.warning("فایل active_sessions.json خراب یا غیرقابل‌خواندنه؛ نادیده گرفته شد.")
        return {}


def save_all(sessions: dict):
    """
    همه‌ی session‌های فعال رو (نسخه‌ی قابل‌serialize‌شون) با نوشتن اتمیک
    (نوشتن در یک فایل موقت و rename) ذخیره می‌کنه، تا اگه دقیقاً وسط نوشتن
    ربات crash کنه، فایل قبلی خراب نشه (rename در سطح فایل‌سیستم اتمیکه).
    """
    try:
        data = {uid: _serialize_session(s) for uid, s in sessions.items()}
        tmp_path = SESSIONS_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, SESSIONS_FILE)
    except (OSError, TypeError, ValueError):
        # این تابع صرفاً یک لایه‌ی best-effort برای بازیابی بعد از کرش
        # است (به docstring بالای فایل مراجعه کن)؛ نباید هیچ‌وقت باعث
        # کرش خود ربات بشه. علاوه بر خطاهای دیسک/فایل (OSError)، اگه یک
        # کلید جدید و غیرقابل‌serialize به _PERSISTABLE_KEYS اضافه بشه
        # (TypeError/ValueError از json.dump)، بازم فقط لاگ می‌کنیم و به
        # جریان عادی برنامه برمی‌گردیم.
        logger.exception("ذخیره‌ی active_sessions.json ناموفق بود")


def clear():
    """فایل ذخیره‌شده رو پاک می‌کنه (مثلاً وقتی همه‌ی session‌ها خالی شدن)."""
    try:
        if os.path.exists(SESSIONS_FILE):
            os.remove(SESSIONS_FILE)
    except OSError:
        logger.exception("حذف active_sessions.json ناموفق بود")


def recover_sessions() -> dict:
    """
    در استارت‌آپ صدا زده می‌شه. برای هر session ذخیره‌شده، فایل‌هایی که واقعاً
    هنوز روی دیسک هستن رو نگه می‌داره و بقیه رو حذف می‌کنه؛ اگه هیچ فایلی از
    یک session باقی نمونده بود، کل اون session دور ریخته می‌شه (چون چیزی
    برای ادامه دادن نداره).

    خروجی: دیکشنری session‌های قابل‌بازیابی، آماده برای جایگزینی مستقیم در
    _sessions، به‌همراه اطلاعات لازم برای اطلاع‌رسانی به کاربر.
    """
    raw = load_all()
    recovered = {}

    for uid, session in raw.items():
        files = session.get("files", [])
        existing_files = [f for f in files if os.path.exists(f.get("path", ""))]

        part_files = session.get("part_files", [])
        existing_parts = [p for p in part_files if os.path.exists(p)] if part_files else []

        if not existing_files and not existing_parts:
            # هیچ فایلی از این session باقی نمونده؛ چیزی برای بازیابی نیست.
            continue

        session["files"] = existing_files
        if part_files:
            session["part_files"] = existing_parts
        recovered[uid] = session

    # فایل رو با نسخه‌ی پاک‌سازی‌شده (بدون session‌های کاملاً ازدست‌رفته)
    # بازنویسی می‌کنیم تا دفعه‌ی بعد دوباره سعی نکنیم چیزی که وجود نداره رو
    # بازیابی کنیم.
    if recovered:
        save_all(recovered)
    else:
        clear()

    return recovered
