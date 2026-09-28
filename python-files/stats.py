import sqlite3
import os
import time
import asyncio
import logging

logger = logging.getLogger(__name__)

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_stats.db")

# ─── اتصال سراسری ────────────────────────────────────────────────
# به‌جای باز/بستن یک کانکشن جدید در هر فراخوانی (که overhead غیرضروری داره
# و در نوشتن هم‌زمان می‌تونه به "database is locked" منجر بشه)، یک کانکشن
# سراسری با WAL mode نگه می‌داریم. WAL اجازه‌ی خواندن هم‌زمان با نوشتن رو
# می‌ده و شدیداً ریسک قفل‌شدن رو کم می‌کنه.
# check_same_thread=False چون این کانکشن هم از event loop اصلی (thread اصلی)
# و هم از executor threadهایی که run_in_executor می‌سازه صدا زده می‌شه.
_con = sqlite3.connect(DB_PATH, check_same_thread=False)
_con.execute("PRAGMA journal_mode=WAL")
_con.execute("PRAGMA synchronous=NORMAL")

_db_initialized = False

def init_db():
    """
    جدول و ایندکس‌ها رو در صورت نبود می‌سازه. به‌صورت صریح در ابتدای اجرای
    برنامه (main.py) صدا زده می‌شه. علاوه بر این، هر تابع log_*/get_stats هم
    قبل از اولین استفاده‌ی خودش این تابع رو (idempotent) صدا می‌زنه تا اگه
    این ماژول مستقیم و بدون عبور از main.py استفاده بشه (مثلاً در تست یا
    اجرای دستی یک اسکریپت دیگه)، باز هم جدول از قبل ساخته شده باشه؛ یعنی
    دیگه ساخت دیتابیس یک side-effect پنهان در زمان import نیست، بلکه فقط
    هنگام نیاز واقعی (lazy) انجام می‌شه.
    """
    global _db_initialized
    with _con:
        _con.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL    NOT NULL,
                event     TEXT    NOT NULL,
                size_mb   REAL    DEFAULT 0,
                count     INTEGER DEFAULT 1
            )
        """)
        # ایندکس روی (event, ts) چون رایج‌ترین query دقیقاً همین دو ستون رو
        # فیلتر می‌کنه؛ بدون این ایندکس با رشد جدول، هر کوئری آمار به یک
        # full table scan تبدیل می‌شه.
        _con.execute("""
            CREATE INDEX IF NOT EXISTS idx_events_event_ts
            ON events (event, ts)
        """)
    _db_initialized = True


def _ensure_db():
    if not _db_initialized:
        init_db()


# ─── ثبت رویدادها ──────────────────────────────────────────────
# این توابع sync هستن (برای سازگاری با کدی که همین الان از جاهای مختلف
# telegram_bot.py صداشون می‌زنه)، ولی چون WAL فعاله و insert روی این جدول
# کوچیک هست، عملاً میکروثانیه طول می‌کشن و بلاک قابل‌توجهی برای event loop
# ایجاد نمی‌کنن. برای فراخوانی از کد async و بدون هیچ بلاک شدنی،
# نسخه‌ی async هر تابع هم در ادامه (log_download_async و ...) اضافه شده.

def log_download(size_mb: float):
    """دانلود فایل از تلگرام"""
    _ensure_db()
    try:
        with _con:
            _con.execute("INSERT INTO events (ts, event, size_mb) VALUES (?, 'download', ?)",
                         (time.time(), size_mb))
    except sqlite3.Error:
        logger.exception("ثبت آمار دانلود ناموفق بود")

def log_send(size_mb: float):
    """ارسال فایل به روبیکا"""
    _ensure_db()
    try:
        with _con:
            _con.execute("INSERT INTO events (ts, event, size_mb) VALUES (?, 'send', ?)",
                         (time.time(), size_mb))
    except sqlite3.Error:
        logger.exception("ثبت آمار ارسال ناموفق بود")

def log_archive(size_mb: float, parts: int = 1):
    """ساخت آرشیو 7z یا زیپ"""
    _ensure_db()
    try:
        with _con:
            _con.execute("INSERT INTO events (ts, event, size_mb, count) VALUES (?, 'archive', ?, ?)",
                         (time.time(), size_mb, parts))
    except sqlite3.Error:
        logger.exception("ثبت آمار آرشیو ناموفق بود")


# ─── نسخه‌ی async (برای فراخوانی بدون بلاک کردن event loop) ───────
# این توابع کار I/O سنکرون sqlite رو در یک thread جدا (executor پیش‌فرض
# asyncio) اجرا می‌کنن تا حتی همون میکروثانیه‌های insert هم event loop
# اصلی ربات رو بلاک نکنن. استفاده از این نسخه‌ها اختیاریه و به‌مرور زمان
# می‌تونن جایگزین نسخه‌ی sync در کد فراخوانی‌کننده بشن.

async def log_download_async(size_mb: float):
    await asyncio.get_event_loop().run_in_executor(None, log_download, size_mb)

async def log_send_async(size_mb: float):
    await asyncio.get_event_loop().run_in_executor(None, log_send, size_mb)

async def log_archive_async(size_mb: float, parts: int = 1):
    await asyncio.get_event_loop().run_in_executor(None, log_archive, size_mb, parts)


# ─── خواندن آمار ───────────────────────────────────────────────

def _since(days: int) -> float:
    return time.time() - days * 86400

def get_stats(days: int) -> dict:
    _ensure_db()
    since = _since(days)

    def q(event, col="size_mb"):
        row = _con.execute(
            f"SELECT COUNT(*), SUM({col}) FROM events WHERE event=? AND ts>=?",
            (event, since)
        ).fetchone()
        return (row[0] or 0, row[1] or 0.0)

    dl_cnt,  dl_mb   = q("download")
    snd_cnt, snd_mb  = q("send")
    arc_cnt, arc_mb  = q("archive")
    arc_parts        = _con.execute(
        "SELECT SUM(count) FROM events WHERE event='archive' AND ts>=?", (since,)
    ).fetchone()[0] or 0

    return {
        "download":  {"count": dl_cnt,  "gb": dl_mb  / 1024},
        "send":      {"count": snd_cnt, "gb": snd_mb  / 1024},
        "archive":   {"count": arc_cnt, "gb": arc_mb  / 1024, "parts": arc_parts},
    }

def format_stats_text() -> str:
    lines = ["📊 آمار ربات\n" + "─" * 22]

    for days, label in [(1, "امروز"), (7, "۷ روز گذشته"), (30, "۳۰ روز گذشته")]:
        s = get_stats(days)
        dl  = s["download"]
        snd = s["send"]
        arc = s["archive"]
        lines.append(
            f"\n📅 {label}:\n"
            f"  ⬇️ دانلود:  {dl['count']} فایل — {dl['gb']:.2f} GB\n"
            f"  📤 ارسال:   {snd['count']} فایل — {snd['gb']:.2f} GB\n"
            f"  🗜 آرشیو:   {arc['count']} بار — {arc['parts']} پارت"
        )

    return "\n".join(lines)
