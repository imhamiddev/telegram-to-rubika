import os
import asyncio
import time
import logging
from pyrogram import Client as PyroClient, StopTransmission
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_TOKEN, DOWNLOAD_DIR
from rubika_bot import random_filename, cleanup

logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(BASE_DIR, "tg_bot_session")

PROGRESS_INTERVAL = 3.0   # حداقل فاصله‌ی دو آپدیت پیشرفت (ثانیه)

_pyro_client = None


async def get_pyro_client():
    global _pyro_client
    if _pyro_client is None:
        # pyrogram با bot token — بدون محدودیت ۲۰MB دانلود Bot API
        _pyro_client = PyroClient(
            SESSION_PATH,
            api_id=TELEGRAM_API_ID,
            api_hash=TELEGRAM_API_HASH,
            bot_token=TELEGRAM_TOKEN,
        )
    if not _pyro_client.is_connected:
        await _pyro_client.start()
    return _pyro_client


async def download_large_file(message, original_filename, progress_callback=None, is_cancelled=None):
    """
    فایل بزرگ رو با Pyrogram دانلود می‌کنه.

    is_cancelled: تابع بدون آرگومان که اگه True برگردونه، دانلود فوراً (در اولین
    chunk بعدی) با StopTransmission متوقف و فایل ناقص پاک می‌شه.
    اگه لغو بشه asyncio.CancelledError پرتاب می‌شه.
    """
    client = await get_pyro_client()
    rand_name = random_filename(original_filename)
    save_path = os.path.join(DOWNLOAD_DIR, rand_name)

    last_report = [0.0]

    async def progress(current, total):
        # چک لغو در هر chunk انجام می‌شه، نه فقط وقتی پیام وضعیت آپدیت می‌شه
        if is_cancelled and is_cancelled():
            raise StopTransmission()
        if not total:
            return
        now = time.monotonic()
        if now - last_report[0] < PROGRESS_INTERVAL and current < total:
            return
        last_report[0] = now
        percent = int(current / total * 100)
        bar = "█" * (percent // 10) + "░" * (10 - percent // 10)
        if progress_callback:
            await progress_callback(
                f"⬇️ دانلود: [{bar}] {percent}%\n"
                f"{current // (1024 * 1024)}MB از {total // (1024 * 1024)}MB"
            )

    chat_id = message.chat.id
    msg_id = message.message_id

    logger.debug(f"دریافت فایل از pyrogram: chat_id={chat_id}, msg_id={msg_id}")

    pyro_msg = await client.get_messages(chat_id, msg_id)

    if pyro_msg is None or pyro_msg.empty:
        raise Exception("پیام پیدا نشد")

    result = await client.download_media(pyro_msg, file_name=save_path, progress=progress)

    if is_cancelled and is_cancelled():
        cleanup(save_path, result if isinstance(result, str) else None)
        raise asyncio.CancelledError()
    if not result:
        cleanup(save_path)
        raise Exception("دانلود ناموفق بود")

    return result if isinstance(result, str) else save_path
