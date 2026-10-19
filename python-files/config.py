import os
from dotenv import load_dotenv

load_dotenv()


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
ALLOWED_USER_ID = _int_env("ALLOWED_USER_ID", 0)

TELEGRAM_API_ID = _int_env("TELEGRAM_API_ID", 0)
TELEGRAM_API_HASH = os.getenv("TELEGRAM_API_HASH", "")
TELEGRAM_PHONE = os.getenv("TELEGRAM_PHONE", "")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DOWNLOAD_DIR = os.getenv("DOWNLOAD_DIR", os.path.join(BASE_DIR, "downloads")) + "/"
os.makedirs(DOWNLOAD_DIR, exist_ok=True)

MAX_SIZE_MB = _int_env("MAX_SIZE_MB", 2000)

# حداقل فضای خالی دیسک (MB) که بعد از هر دانلود باید باقی بمونه
MIN_FREE_MB = _int_env("MIN_FREE_MB", 300)

# ─── تنظیمات دانلودر (همه اختیاری) ─────────────────────────────
# فایل کوکی (فرمت Netscape) برای اینستاگرام/یوتیوب/... ؛ هم yt-dlp و هم gallery-dl ازش استفاده می‌کنن
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
# پروکسی مثل http://127.0.0.1:8080 یا socks5://127.0.0.1:1080
PROXY = os.getenv("PROXY", "").strip()
# رانتایم جاوااسکریپت برای یوتیوب: خالی = تشخیص خودکار (deno یا node)
JS_RUNTIME = os.getenv("JS_RUNTIME", "").strip()
# حداکثر تعداد فایل برای یک لینک گالری (جلوگیری از دانلود کل یک پروفایل)
GALLERY_MAX_FILES = _int_env("GALLERY_MAX_FILES", 20)

# ─── رمز ثابت آرشیو (اختیاری) ──────────────────────────────────
# اگه تنظیم بشه، همیشه همین رمز برای آرشیو «زیپ + رمز» استفاده می‌شه و **توی کپشن
# روبیکا نوشته نمی‌شه** (پس رمزنگاری واقعاً محافظت می‌کنه). خالی = رمز رندوم برای هر آرشیو
# که توی کپشن همون پیام میاد.
ARCHIVE_PASSWORD = os.getenv("ARCHIVE_PASSWORD", "").strip()
MIN_ARCHIVE_PASSWORD_LEN = 12
if ARCHIVE_PASSWORD and len(ARCHIVE_PASSWORD) < MIN_ARCHIVE_PASSWORD_LEN:
    raise ValueError(
        f"ARCHIVE_PASSWORD باید حداقل {MIN_ARCHIVE_PASSWORD_LEN} کاراکتر باشه "
        f"(الان {len(ARCHIVE_PASSWORD)} کاراکتره)."
    )
