import logging
from telegram_bot import run_telegram_bot

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
# کتابخونه‌های httpx/telegram خیلی پرحرف هستن روی INFO، ببریمشون رو WARNING
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)

if __name__ == "__main__":
    run_telegram_bot()
