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