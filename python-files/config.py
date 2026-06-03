import os
from dotenv import load_dotenv
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
FIXED_PASSWORD = os.getenv("FIXED_PASSWORD", "")

TELEGRAM_API_ID = int(os.getenv("TELEGRAM_API_ID", "0"))
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")

DOWNLOAD_DIR = "/home/lwrixcmp/downloads/"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_SIZE_MB = 2000
