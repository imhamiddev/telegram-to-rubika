import os
from pyrogram import Client as PyroClient
from config import TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_TOKEN, DOWNLOAD_DIR
from rubika_bot import random_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
SESSION_PATH = os.path.join(BASE_DIR, "tg_bot_session")

_pyro_client = None

async def get_pyro_client():
    global _pyro_client
    if _pyro_client is None:
        # pyrogram با bot token — بدون محدودیت 20MB برای دانلود
        _pyro_client = PyroClient(
            SESSION_PATH,
            api_id=TELEGRAM_API_ID,
            api_hash=TELEGRAM_API_HASH,
            bot_token=TELEGRAM_TOKEN,
        )
    if not _pyro_client.is_connected:
        await _pyro_client.start()
    return _pyro_client

async def download_large_file(message, original_filename, progress_callback=None):
    client = await get_pyro_client()
    rand_name = random_filename(original_filename)
    save_path = os.path.join(DOWNLOAD_DIR, rand_name)

    last_reported = [0]

    async def progress(current, total):
        percent = int(current / total * 100)
        if percent - last_reported[0] >= 15 or percent == 100:
            last_reported[0] = percent
            bar = "█" * (percent // 10) + "░" * (10 - percent // 10)
            if progress_callback:
                await progress_callback(
                    f"⬇️ دانلود: [{bar}] {percent}%\n"
                    f"{current//(1024*1024)}MB از {total//(1024*1024)}MB"
                )

    chat_id = message.chat.id
    msg_id = message.message_id

    print(f"[DEBUG] chat_id={chat_id}, msg_id={msg_id}")

    pyro_msg = await client.get_messages(chat_id, msg_id)

    if pyro_msg is None or pyro_msg.empty:
        raise Exception("پیام پیدا نشد")

    await client.download_media(pyro_msg, file_name=save_path, progress=progress)

    return save_path