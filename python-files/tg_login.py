"""
tg_login.py — لاگین یک‌بار به یوزراکانت تلگرام (اختیاری / پیشرفته)

⚠️ نکته‌ی مهم:
ربات در حالت عادی این سشن یوزراکانتی رو استفاده نمی‌کنه. دانلود فایل‌های
بزرگ (`tg_client.py`) با همون TELEGRAM_TOKEN ربات و پروتکل Pyrogram/MTProto
انجام می‌شه که خودش محدودیت ۲۰MB نسخه‌ی HTTP بات‌ها رو نداره؛ بنابراین اجرای
این اسکریپت برای کارکرد عادی ربات لازم نیست.

این اسکریپت فقط برای کسایی مفیده که بخوان قابلیتی رو توسعه بدن که نیاز به
دسترسی سطح یوزراکانت (نه بات) داره؛ مثلاً خوندن پیام‌های چت‌هایی که ربات در
اونا عضو نیست. اجرا کردنش یک فایل session با نام tg_user_session می‌سازه که
فعلاً در هیچ فایل دیگه‌ای از پروژه استفاده نمی‌شه.
"""
import asyncio
from pyrogram import Client
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_PHONE
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(BASE_DIR, "tg_user_session")

async def main():
    async with Client(
        SESSION_PATH,
        api_id=TELEGRAM_API_ID,
        api_hash=TELEGRAM_API_HASH,
        phone_number=TELEGRAM_PHONE,
    ) as client:
        me = await client.get_me()
        print(f"✅ لاگین موفق! خوش اومدی {me.first_name}")

asyncio.run(main())