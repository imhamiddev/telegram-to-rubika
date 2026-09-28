import os
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads")) + "/"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_SIZE_MB = int(os.getenv("MAX_SIZE_MB", "2000"))
