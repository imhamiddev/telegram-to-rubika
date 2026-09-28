import logging
from telegram_bot import run_telegram_bot
from stats import init_db

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
    # ساخت جدول/ایندکس‌های آمار (در صورت نبود) — صریح و یک‌بار در شروع برنامه،
    # نه به‌صورت ضمنی هنگام import ماژول stats.
    init_db()
    run_telegram_bot()
