"""
rubika_bot.py — توابع کمکی مشترک (نام فایل تصادفی، پسورد، پاک‌سازی، کپشن شمسی).

ارسال واقعی فایل‌ها به روبیکا در telegram_bot.do_send انجام می‌شه.
(صف/Worker و زیپ AESZipFile که قبلاً این‌جا بودن و هیچ‌جا استفاده نمی‌شدن حذف شدن.)
"""
import os
import secrets
import string

import jdatetime

PART_SIZE_MB = 95          # حجم هر پارت
SPLIT_THRESHOLD_MB = 100   # بالای این تقسیم میشه


def get_shamsi_caption():
    now = jdatetime.datetime.now()
    return f"{now.year}/{now.month:02d}/{now.day:02d} - {now.hour:02d}:{now.minute:02d}"


def generate_password(length=14):
    # فقط حروف و عدد - بدون کاراکتر خاص که ممکنه توی بعضی برنامه‌ها مشکل بده
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))


def random_filename(original_filename):
    ext = os.path.splitext(original_filename)[1]
    return secrets.token_hex(10) + ext


def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass
