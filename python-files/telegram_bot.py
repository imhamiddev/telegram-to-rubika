import os
import re
import sys
import glob
import math
import shutil
import asyncio
import time
import logging
import subprocess
from contextlib import contextmanager
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, CommandHandler
)
from config import (
    TELEGRAM_TOKEN, DOWNLOAD_DIR, MAX_SIZE_MB, ALLOWED_USER_ID, COOKIES_FILE, ARCHIVE_PASSWORD,
)
from rubika_bot import random_filename, cleanup, PART_SIZE_MB, generate_password
from rubpy import Client
import jdatetime
from downloader import (
    DownloadUserError,
    NotVideoError,
    download_direct_link,
    download_gallery,
    download_video_from_page,
    ensure_free_space,
    free_space_mb,
    gallery_available,
    get_video_info,
    is_direct_link,
    is_gallery_domain,
    is_gallery_first,
)
from stats import log_download, log_send, log_archive, format_stats_text  # ← آمار
import session_store

logger = logging.getLogger(__name__)

RUBIKA_SESSION          = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'my_session')
LARGE_FILE_THRESHOLD_MB = 20
SPLIT_THRESHOLD_MB      = 100
FORWARD_COLLECT_SECONDS = 5
MAX_AUTO_RETRY          = 15

GENERIC_ERROR_MSG = "❌ یه خطا پیش اومد. لطفاً دوباره امتحان کن."


def safe_error_text(prefix: str, exc: Exception) -> str:
    """
    پیام خطا رو لاگ می‌کنه (با جزئیات کامل) ولی به کاربر فقط یه پیام
    عمومی نشون می‌ده تا مسیر فایل/جزئیات داخلی سرور لو نره.
    استثنا: DownloadUserError (مثل «حجم زیاد» یا «دیسک پر») که پیامش از قبل امنه.
    """
    if isinstance(exc, DownloadUserError):
        logger.warning("%s %s", prefix, exc)
        return f"{prefix}\n{exc}"
    logger.exception(prefix)
    return f"{prefix}\n{GENERIC_ERROR_MSG}"


_sessions       = {}
_forward_timers = {}
_link_sessions  = {}
_cancel_flags   = {}   # کلید uid → لغو فایل/ارسال ؛ کلید "link:<uid>" → لغو دانلود لینک
_busy           = {}   # uid → تعداد عملیات طولانی در حال اجرا


@contextmanager
def busy(uid):
    """مشخص می‌کنه برای این کاربر یک عملیات طولانی (دانلود/ارسال) در حال اجراست."""
    _busy[uid] = _busy.get(uid, 0) + 1
    try:
        yield
    finally:
        _busy[uid] = _busy.get(uid, 1) - 1
        if _busy[uid] <= 0:
            _busy.pop(uid, None)


def is_busy(uid) -> bool:
    return _busy.get(uid, 0) > 0


def link_cancelled(uid) -> bool:
    return bool(_cancel_flags.get(f"link:{uid}"))


def _persist_sessions():
    """
    وضعیت فعلی همه‌ی session‌ها رو روی دیسک ذخیره می‌کنه (فقط متادیتای
    قابل‌serialize؛ به session_store.py مراجعه کن). این تابع باید بعد از هر
    تغییری در _sessions که می‌خوایم در برابر ری‌استارت دووم بیاره صدا زده
    بشه: اضافه/حذف شدن فایل، شروع ارسال، پیشرفت ارسال (start_from)، و غیره.
    نوشتن روی دیسک ارزون و سریعه (JSON کوچیک)، پس صدا زدنش بعد از هر تغییر
    مشکلی از نظر کارایی ایجاد نمی‌کنه.
    """
    session_store.save_all(_sessions)


def is_allowed(user_id: int) -> bool:
    """
    فقط ALLOWED_USER_ID اجازه‌ی استفاده از ربات رو داره.
    اگه ALLOWED_USER_ID تنظیم نشده باشه (۰)، به‌صورت پیش‌فرض دسترسی رد میشه
    تا ربات به‌اشتباه برای همه باز نمونه.
    """
    return bool(ALLOWED_USER_ID) and user_id == ALLOWED_USER_ID


async def deny_access(update: Update):
    try:
        await update.effective_message.reply_text("❌ دسترسی ندارید.")
    except Exception:
        pass


def smart_delay(file_size_mb: float) -> int:
    if file_size_mb < 10:    return 10
    elif file_size_mb < 50:  return 30
    elif file_size_mb < 100: return 60
    else:                    return 120


MAIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [[
        KeyboardButton("🔗 دانلود از لینک", style="primary"),
        KeyboardButton("📊 وضعیت ربات",     style="primary"),
    ]],
    resize_keyboard=True,
)

def cancel_keyboard(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ لغو و حذف همه", callback_data=f"cancel:{uid}", style="danger")]
    ])

def link_cancel_keyboard(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ لغو", callback_data=f"cancel_ytdlp:{uid}", style="danger")]
    ])


def more_keyboard(uid):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("➕ فایل دیگه دارم", callback_data=f"more:{uid}", style="primary"),
            InlineKeyboardButton("✅ تموم شد",         callback_data=f"done:{uid}", style="success"),
        ],
        [InlineKeyboardButton("❌ لغو", callback_data=f"cancel:{uid}", style="danger")]
    ])

def send_keyboard(uid):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("معمولی",     callback_data=f"send_normal:{uid}", style="primary"),
            InlineKeyboardButton("زیپ",        callback_data=f"send_zip:{uid}", style="primary"),
            InlineKeyboardButton("زیپ + رمز",   callback_data=f"send_safe:{uid}", style="success"),
        ],
        [InlineKeyboardButton("❌ لغو", callback_data=f"cancel:{uid}", style="danger")]
    ])


def get_shamsi():
    now = jdatetime.datetime.now()
    return f"{now.year}/{now.month:02d}/{now.day:02d} - {now.hour:02d}:{now.minute:02d}"

def short_name(name, n=15):
    return name[:n] + "..." if len(name) > n else name

def bar(pct, width=10):
    filled = int(width * pct / 100)
    return "█" * filled + "░" * (width - filled)

def get_file_info(message):
    if message.document: return message.document, message.document.file_name
    elif message.video:  return message.video,    f"video_{message.video.file_id}.mp4"
    elif message.audio:
        f = message.audio
        return f, f.file_name or f"audio_{f.file_id}.mp3"
    elif message.photo:
        f = message.photo[-1]
        return f, f"photo_{f.file_id}.jpg"
    elif message.voice:
        f = message.voice
        return f, f"voice_{f.file_id}.ogg"
    return None, None

def files_summary(files):
    total = sum(os.path.getsize(f["path"]) for f in files if os.path.exists(f["path"]))
    size_str = f"{total/(1024*1024):.1f}MB"
    names = "\n".join(
        f"  • {short_name(f['name'])} ({os.path.getsize(f['path'])/(1024*1024):.1f}MB)"
        for f in files if os.path.exists(f["path"])
    )
    return names, size_str, total / (1024 * 1024)

def cleanup_session(uid):
    session = _sessions.pop(uid, {})
    for f in session.get("files", []):
        cleanup(f["path"])
    for p in session.get("part_files", []):
        cleanup(p)
    _cancel_flags.pop(uid, None)
    if uid in _forward_timers:
        _forward_timers[uid].cancel()
        _forward_timers.pop(uid, None)
    _persist_sessions()


_SEVENZIP_CANDIDATES = [
    os.getenv("SEVENZIP_BIN", ""),
    os.path.expanduser("~/.local/bin/7za"),
    "/usr/local/bin/7za",
    "/usr/bin/7za",
    "7za",   # جستجو در PATH
    "7z",    # بعضی توزیع‌ها فقط 7z دارن نه 7za
]

def find_sevenzip_bin():
    """
    مسیر باینری 7za/7z رو پیدا می‌کنه: اول SEVENZIP_BIN از .env، بعد چند مسیر
    رایج نصب، و در نهایت جستجو در PATH سیستم. اگه هیچ‌کدوم پیدا نشه None
    برمی‌گردونه تا با یه پیام خطای واضح به کاربر گفته بشه (به‌جای کرش خام).
    """
    for candidate in _SEVENZIP_CANDIDATES:
        if not candidate:
            continue
        if os.path.isabs(candidate):
            if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
                return candidate
        else:
            found = shutil.which(candidate)
            if found:
                return found
    return None

SEVENZIP_BIN = find_sevenzip_bin()

def create_7z(output_base, password, part_size_mb, *file_paths):
    if not SEVENZIP_BIN:
        raise Exception(
            "ابزار 7za/7z روی سرور پیدا نشد. یا آن را نصب کن (مثلاً: apt install p7zip-full) "
            "یا مسیرش رو در متغیر SEVENZIP_BIN در فایل .env مشخص کن."
        )

    pattern = glob.escape(output_base) + ".7z*"
    for f in glob.glob(pattern):
        try:
            os.remove(f)
        except OSError:
            pass

    cmd = [SEVENZIP_BIN, "a", "-mx=0", "-bsp0", "-bso0"]

    if password:
        # توجه: آرگومان -p روی سیستم‌های چندکاربره در ps دیده می‌شه (برای سرور شخصی مشکلی نیست).
        # -mhe=on اسم فایل‌های داخل آرشیو رو هم رمزنگاری می‌کنه.
        cmd += [f"-p{password}", "-mhe=on"]

    if part_size_mb:
        cmd.append(f"-v{part_size_mb}m")

    cmd.append(output_base + ".7z")
    cmd.extend(fp for fp in file_paths if os.path.exists(fp))

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        for f in glob.glob(pattern):      # خروجی ناقص رو جا نذار
            try:
                os.remove(f)
            except OSError:
                pass
        raise Exception(f"خطا در 7za: {result.stderr or result.stdout}")

    parts = sorted(glob.glob(glob.escape(output_base) + ".7z.*"))
    if not parts:
        single = output_base + ".7z"
        if os.path.exists(single):
            return [single]
        raise Exception("فایل 7z ساخته نشد!")
    return parts


async def react(message, emoji: str, big: bool = False):
    try:
        from telegram import ReactionTypeEmoji
        await message.set_reaction([ReactionTypeEmoji(emoji=emoji)], is_big=big)
    except Exception:
        pass


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.message.from_user.id):
        await deny_access(update)
        return
    first = update.message.from_user.first_name or "کاربر"
    await update.message.reply_text(
        f"👋 سلام {first}!\n\n"
        "به ربات انتقال فایل خوش اومدی 🚀\n\n"
        "📌 فایلت رو بفرست تا دانلود و به روبیکا ارسال بشه.\n"
        "🔗 یا لینک یوتیوب/اینستاگرام/توییتر/... رو بفرست.\n"
        "❌ /cancel لغو همه‌چیز  |  🔄 /update به‌روزرسانی دانلودرها\n"
        "⬇️ از دکمه‌های پایین هم می‌تونی استفاده کنی:",
        reply_markup=MAIN_REPLY_KEYBOARD,
    )
    await update.message.reply_text(
        "Developed by:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("Hamid", url="https://t.me/imhamiddev", style="success")]
        ])
    )


async def bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.message.from_user.id):
        await deny_access(update)
        return
    uid    = str(update.message.from_user.id)
    active = len(_sessions)
    disk_files = glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
    disk_mb = sum(os.path.getsize(f) for f in disk_files if os.path.isfile(f)) / (1024*1024)
    session_info = ""
    if uid in _sessions:
        files = _sessions[uid].get("files", [])
        parts = _sessions[uid].get("part_files", [])
        _, sz, _ = files_summary(files) if files else ("", "0MB", 0)
        session_info = f"\n\n🗂 جلسه فعال شما: {len(files)} فایل ({sz})"
        if parts:
            session_info += f" + {len(parts)} پارت آرشیو"

    stats_text = format_stats_text()

    await update.message.reply_text(
        f"🟢 ربات آنلاین\n"
        f"👥 جلسه‌های فعال: {active}\n"
        f"💾 فضای موقت: {disk_mb:.1f} MB\n"
        f"💽 فضای خالی دیسک: {free_space_mb() / 1024:.1f} GB\n"
        f"🍪 کوکی: {'✅' if COOKIES_FILE and os.path.isfile(COOKIES_FILE) else '—'}"
        f"{session_info}\n\n"
        f"{stats_text}",
        reply_markup=MAIN_REPLY_KEYBOARD,
    )


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/cancel — هر عملیات در حال اجرا رو متوقف و فایل‌های موقت رو پاک می‌کنه."""
    if not is_allowed(update.effective_user.id):
        await deny_access(update)
        return
    uid = str(update.effective_user.id)
    _link_sessions.pop(uid, None)
    if is_busy(uid):
        # عملیات در حال اجرا خودش با دیدن فلگ متوقف و پاک‌سازی می‌کنه
        _cancel_flags[uid] = True
        _cancel_flags[f"link:{uid}"] = True
        await asyncio.sleep(1.5)
    if uid in _sessions:
        cleanup_session(uid)
    _cancel_flags.pop(uid, None)
    _cancel_flags.pop(f"link:{uid}", None)
    await update.message.reply_text(
        "❌ لغو شد. همه فایل‌های موقت پاک شدن.", reply_markup=MAIN_REPLY_KEYBOARD
    )


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/update — yt-dlp و gallery-dl رو به آخرین نسخه می‌رسونه (سایت‌ها مدام تغییر می‌کنن)."""
    if not is_allowed(update.effective_user.id):
        await deny_access(update)
        return
    msg = await update.message.reply_text("🔄 در حال به‌روزرسانی yt-dlp و gallery-dl ...")
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]", "gallery-dl",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=300)
    except asyncio.TimeoutError:
        proc.kill()
        await msg.edit_text("❌ به‌روزرسانی بیش از ۵ دقیقه طول کشید و متوقف شد.")
        return
    if proc.returncode != 0:
        logger.error("pip update failed:\n%s", out.decode(errors="replace")[-1500:])
        await msg.edit_text("❌ به‌روزرسانی ناموفق بود (جزئیات توی لاگ).")
        return
    from importlib import metadata
    def ver(pkg):
        try:
            return metadata.version(pkg)
        except metadata.PackageNotFoundError:
            return "؟"
    await msg.edit_text(f"✅ به‌روز شد\nyt-dlp: {ver('yt-dlp')}\ngallery-dl: {ver('gallery-dl')}")


async def ask_for_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.message.from_user.id):
        await deny_access(update)
        return
    uid = str(update.message.from_user.id)
    _link_sessions[uid] = {"waiting": True}
    await update.message.reply_text(
        "🔗 لینک رو بفرست:\n\n"
        "• ویدیو/صوت: یوتیوب، اینستاگرام، توییتر/X، تیک‌تاک، فیسبوک، ...\n"
        "• عکس/گالری: اینستاگرام، توییتر/X، پینترست، ردیت، ...\n"
        "• لینک مستقیم فایل\n\n"
        "❌ برای لغو /cancel بزن",
        reply_markup=MAIN_REPLY_KEYBOARD,
    )


def _guard_link_start(uid) -> str:
    """اگه الان نباید دانلود لینک جدیدی شروع بشه، دلیلش رو برمی‌گردونه."""
    session = _sessions.get(uid) or {}
    if session.get("sending") or session.get("part_files"):
        return "⚠️ یک ارسال/آرشیو در حال انجامه؛ اول اون تموم بشه یا /cancel بزن."
    if session.get("collecting_forwards"):
        return "⚠️ دارم فایل‌های forward شده رو جمع می‌کنم؛ چند ثانیه صبر کن."
    return ""


async def _register_downloaded(uid, paths, status_msg, header):
    """فایل‌های دانلودشده رو به session اضافه و خلاصه‌شون رو نشون می‌ده."""
    if uid not in _sessions:
        _sessions[uid] = {"files": [], "waiting": False}
    for p in paths:
        _sessions[uid]["files"].append({"path": p, "name": os.path.basename(p)})
        log_download(os.path.getsize(p) / (1024 * 1024))
    _persist_sessions()
    files = _sessions[uid]["files"]
    names, total_size, _ = files_summary(files)
    await status_msg.edit_text(
        f"{header}\n\n📦 فایل‌های آماده ({len(files)} — {total_size}):\n{names}\n\nفایل دیگه‌ای؟",
        reply_markup=more_keyboard(uid),
    )


async def handle_link_download(uid, url, message, context):
    reason = _guard_link_start(uid)
    if reason:
        await message.reply_text(reason)
        return

    _cancel_flags[f"link:{uid}"] = False
    cancel_check = lambda: link_cancelled(uid)

    status_msg = await message.reply_text(
        f"🔍 در حال بررسی لینک...\n🔗 {url[:60]}", reply_markup=link_cancel_keyboard(uid)
    )

    async def progress(text):
        try:
            await status_msg.edit_text(f"{text}\n🔗 {url[:60]}", reply_markup=link_cancel_keyboard(uid))
        except Exception:
            pass

    async def try_gallery(note):
        await progress(note)
        paths = await download_gallery(uid, url, progress, cancel_check)
        await _register_downloaded(uid, paths, status_msg, f"✅ {len(paths)} فایل دانلود شد")

    with busy(uid):
        try:
            # ۱) لینک مستقیم فایل
            if is_direct_link(url):
                await progress("⬇️ دانلود لینک مستقیم...")
                try:
                    save_path = await download_direct_link(uid, url, progress, cancel_check)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    await status_msg.edit_text(safe_error_text("❌ دانلود لینک ناموفق بود.", e))
                    return
                await _register_downloaded(
                    uid, [save_path], status_msg,
                    f"✅ دانلود شد ({os.path.getsize(save_path) / (1024 * 1024):.1f}MB)",
                )
                return

            # ۲) سایت‌های عکس/گالری: اول gallery-dl
            if is_gallery_first(url) and gallery_available():
                try:
                    await try_gallery("🖼 در حال دانلود گالری...")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.info("gallery-first failed for %r: %s", url, e)

            # ۳) ویدیو با yt-dlp
            await progress("🔍 در حال پیدا کردن ویدیو...")
            try:
                info = await get_video_info(url)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"get_video_info failed for url={url!r}: {e}")
                # ۴) fallback: پست عکس‌دار → gallery-dl
                if gallery_available() and (isinstance(e, NotVideoError) or is_gallery_domain(url)):
                    try:
                        await try_gallery("🖼 ویدیویی پیدا نشد؛ تلاش برای دانلود عکس/گالری...")
                        return
                    except asyncio.CancelledError:
                        raise
                    except Exception as ge:
                        logger.warning("gallery fallback failed for %r: %s", url, ge)
                await status_msg.edit_text(f"❌ ویدیویی پیدا نشد:\n{str(e)[:300]}")
                return

            formats = info["formats"]
            _link_sessions[uid] = {
                "waiting": False, "ytdlp_url": url,
                "ytdlp_formats": formats, "ytdlp_title": info["title"],
                "status_msg_id": status_msg.message_id,
            }
            buttons = []
            for f in formats:
                size_text = f" ({f['filesize_mb']}MB)" if f["filesize_mb"] > 0 else ""
                style = "primary" if f.get("type") == "audio" else "success"
                buttons.append([InlineKeyboardButton(
                    f"{f['label']}{size_text}", callback_data=f"ytdlp:{uid}:{f['format_id']}",
                    style=style,
                )])
            buttons.append([InlineKeyboardButton("❌ لغو", callback_data=f"cancel_ytdlp:{uid}", style="danger")])
            await status_msg.edit_text(
                f"🎬 {info['title'][:40]}\n⏱ {info['duration']}\n\nکیفیت:",
                reply_markup=InlineKeyboardMarkup(buttons),
            )
        except asyncio.CancelledError:
            try:
                await status_msg.edit_text("❌ لغو شد.")
            except Exception:
                pass
        except Exception as e:
            try:
                await status_msg.edit_text(safe_error_text("❌ پردازش لینک ناموفق بود.", e))
            except Exception:
                pass


async def handle_ytdlp_quality_callback(uid, format_id, query):
    session = _link_sessions.get(uid, {})
    url = session.get("ytdlp_url")
    if not url:
        await query.edit_message_text("❌ session منقضی.")
        return
    if session.get("downloading"):
        return   # کلیک دوباره روی دکمه → دانلود دوم شروع نشه
    selected = next((f for f in session.get("ytdlp_formats", []) if f["format_id"] == format_id), None)
    if not selected:
        await query.edit_message_text("❌ گزینه‌ی نامعتبر؛ لینک رو دوباره بفرست.")
        return

    session["downloading"] = True
    _cancel_flags[f"link:{uid}"] = False
    audio_only = selected.get("type") == "audio"

    async def progress_update(text):
        try:
            await query.edit_message_text(f"⬇️ دانلود...\n{text}", reply_markup=link_cancel_keyboard(uid))
        except Exception:
            pass

    await query.edit_message_text("⬇️ دانلود شروع شد...", reply_markup=link_cancel_keyboard(uid))
    with busy(uid):
        try:
            save_path = await download_video_from_page(
                uid, url, selected["selector"], progress_update,
                audio_only=audio_only, cancel_check=lambda: link_cancelled(uid),
            )
        except asyncio.CancelledError:
            _link_sessions.pop(uid, None)
            try:
                await query.edit_message_text("❌ لغو شد.")
            except Exception:
                pass
            return
        except Exception as e:
            _link_sessions.pop(uid, None)
            await query.edit_message_text(safe_error_text("❌ دانلود ویدیو ناموفق بود.", e))
            return
    _link_sessions.pop(uid, None)

    file_mb = os.path.getsize(save_path) / (1024 * 1024)
    if file_mb > MAX_SIZE_MB:          # yt-dlp وقتی حجم رو از قبل نمی‌دونه، سقف رو اعمال نمی‌کنه
        cleanup(save_path)
        await query.edit_message_text(f"❌ حجم فایل ({file_mb:.0f}MB) بیشتر از سقف {MAX_SIZE_MB}MB بود؛ حذف شد.")
        return
    await _register_downloaded(uid, [save_path], query.message, f"✅ ویدیو دانلود شد ({file_mb:.1f}MB)")


def build_multi_progress_text(done_files, current_name, current_pct, current_speed_mb, current_size_mb):
    lines = []
    for f in done_files:
        fmb = os.path.getsize(f["path"]) / (1024*1024) if os.path.exists(f["path"]) else 0
        lines.append(f"  ✅ {short_name(f['name'])} ({fmb:.1f}MB)")
    lines.append(
        f"  ⬇️ {short_name(current_name)} ({current_size_mb:.1f}MB)\n"
        f"     [{bar(current_pct)}] {current_pct}%  ⚡ {current_speed_mb:.2f} MB/s"
    )
    return "\n".join(lines)


async def download_file_with_progress(message, original_filename, context, status_msg, uid, done_files=None):
    if done_files is None:
        done_files = []

    file, _ = get_file_info(message)
    if not file:
        return None

    file_size_mb = (file.file_size or 0) / (1024*1024)
    file_size_b  = file.file_size or 0

    ensure_free_space(file_size_mb)   # DownloadUserError اگه دیسک کافی نباشه

    if file_size_mb > LARGE_FILE_THRESHOLD_MB:
        from tg_client import download_large_file
        async def dl_progress(text):
            done_lines = "\n".join(
                f"  ✅ {short_name(f['name'])} ({os.path.getsize(f['path'])/(1024*1024):.1f}MB)"
                for f in done_files if os.path.exists(f["path"])
            )
            prefix = (done_lines + "\n") if done_lines else ""
            try:
                await status_msg.edit_text(
                    f"{prefix}  ⬇️ {short_name(original_filename)}\n     {text}",
                    reply_markup=cancel_keyboard(uid)
                )
            except Exception:
                pass
        result = await download_large_file(
            message, original_filename, dl_progress,
            is_cancelled=lambda: bool(_cancel_flags.get(uid)),
        )
        log_download(file_size_mb)  # ← ثبت آمار
        return result

    rand_name  = random_filename(original_filename)
    save_path  = os.path.join(DOWNLOAD_DIR, rand_name)
    tg_file    = await context.bot.get_file(file.file_id)
    start_time = time.time()

    async def live_progress():
        while True:
            if _cancel_flags.get(uid):
                return
            await asyncio.sleep(1)
            if not os.path.exists(save_path):
                continue
            elapsed = time.time() - start_time
            done_b  = os.path.getsize(save_path)
            pct     = min(99, int(done_b / file_size_b * 100)) if file_size_b else 50
            speed   = done_b / elapsed / (1024*1024) if elapsed > 0 else 0
            try:
                await status_msg.edit_text(
                    build_multi_progress_text(done_files, original_filename, pct, speed, file_size_mb),
                    reply_markup=cancel_keyboard(uid)
                )
            except Exception:
                pass

    prog_task = asyncio.create_task(live_progress())
    try:
        await tg_file.download_to_drive(save_path)
    except BaseException:
        cleanup(save_path)          # فایل ناقص رو جا نذار
        raise
    finally:
        prog_task.cancel()

    if _cancel_flags.get(uid):
        cleanup(save_path)
        raise asyncio.CancelledError()

    elapsed = time.time() - start_time
    speed   = file_size_mb / elapsed if elapsed > 0 else 0
    try:
        await status_msg.edit_text(
            build_multi_progress_text(done_files, original_filename, 100, speed, file_size_mb),
            reply_markup=cancel_keyboard(uid)
        )
    except Exception:
        pass

    log_download(file_size_mb)  # ← ثبت آمار
    return save_path


async def _process_forward_batch(uid, context):
    """بعد از پنجره‌ی جمع‌آوری، همه‌ی فایل‌های forward شده رو یکی‌یکی دانلود می‌کنه."""
    await asyncio.sleep(FORWARD_COLLECT_SECONDS)
    session = _sessions.get(uid)
    if not session:
        return

    pending = session.pop("pending_forwards", [])
    session["collecting_forwards"] = False
    if not pending:
        cleanup_session(uid)
        return
    pending.sort(key=lambda m: m.message_id)   # ترتیب اصلی پیام‌ها

    status_msg = await pending[0].reply_text(
        f"⬇️ در حال دانلود {len(pending)} فایل...",
        reply_markup=cancel_keyboard(uid)
    )

    downloaded_so_far = []
    failed = []

    for msg in pending:
        if _cancel_flags.get(uid):
            await status_msg.edit_text("❌ لغو شد.")
            cleanup_session(uid)
            return
        f, fname = get_file_info(msg)
        if not f:
            continue
        try:
            save_path = await download_file_with_progress(
                msg, fname, context, status_msg, uid, done_files=downloaded_so_far
            )
            if save_path:
                entry = {"path": save_path, "name": fname}
                session["files"].append(entry)
                downloaded_so_far.append(entry)
                # ثبت پیشرفت بعد از هر فایل؛ اگه وسط کار کرش کنیم فایل‌های قبلی از دست نمی‌رن.
                _persist_sessions()
                await react(msg, "✅", big=False)
        except asyncio.CancelledError:
            await status_msg.edit_text("❌ لغو شد. فایل‌های دانلود‌شده پاک شدن.")
            cleanup_session(uid)
            return
        except Exception as e:
            # یک فایل خراب نباید فایل‌های سالم قبلی رو از بین ببره؛ ردش می‌کنیم و ادامه می‌دیم.
            logger.warning("دانلود %s ناموفق بود: %s", short_name(fname), e)
            failed.append(short_name(fname))
            await react(msg, "❌", big=False)

    if _sessions.get(uid) is not session:
        return   # وسط کار لغو شده

    files = session["files"]
    if not files:
        _sessions.pop(uid, None)
        _persist_sessions()
        await status_msg.edit_text("❌ هیچ فایلی دانلود نشد.")
        return

    names, total_size, _ = files_summary(files)
    failed_note = f"\n\n⚠️ {len(failed)} فایل ناموفق: {', '.join(failed)}" if failed else ""
    _persist_sessions()
    await status_msg.edit_text(
        f"✅ {len(files)} فایل دانلود شد ({total_size})\n\n"
        f"📦 فایل‌ها:\n{names}{failed_note}\n\nفایل دیگه‌ای هم داری؟",
        reply_markup=more_keyboard(uid)
    )


async def _forward_task(uid, context):
    try:
        with busy(uid):
            await _process_forward_batch(uid, context)
    except asyncio.CancelledError:
        raise
    except Exception:
        # بدون این، خطا بی‌صدا گم می‌شد و session تا ری‌استارت گیر می‌کرد.
        logger.exception("پردازش فایل‌های forward شده ناموفق بود")
        cleanup_session(uid)
        try:
            await context.bot.send_message(chat_id=int(uid), text=GENERIC_ERROR_MSG)
        except Exception:
            pass
    finally:
        _forward_timers.pop(uid, None)


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    uid     = str(message.from_user.id)

    if not is_allowed(message.from_user.id):
        await deny_access(update)
        return

    file, original_filename = get_file_info(message)
    if not file:
        await message.reply_text("❌ فرمت پشتیبانی نمیشه.")
        return

    await react(message, "👀", big=True)

    session = _sessions.get(uid)
    if session and not session.get("waiting", False):
        if session.get("collecting_forwards"):
            # فایل‌های forward بعدی هم باید همون سقف حجم رو رعایت کنن
            if (file.file_size or 0) / (1024*1024) > MAX_SIZE_MB:
                await message.reply_text(
                    f"❌ «{short_name(original_filename)}» بزرگ‌تر از {MAX_SIZE_MB}MB بود و نادیده گرفته شد."
                )
                return
            session["pending_forwards"].append(message)
            return
        # این فایل نادیده گرفته می‌شه چون یک session باز دیگه (مثلاً در حال
        # ارسال به روبیکا یا منتظر انتخاب "زیپ/معمولی/زیپ+رمز") وجود داره.
        await message.reply_text(
            "⚠️ یک عملیات دیگه در حال انجامه (مثلاً ارسال به روبیکا یا انتخاب حالت ارسال).\n"
            "این فایل ذخیره نشد — لطفاً منتظر بمون تا عملیات فعلی تموم بشه یا /cancel بزن، "
            "بعد دوباره همین فایل رو بفرست."
        )
        return

    file_size_mb = (file.file_size or 0) / (1024*1024)
    if file_size_mb > MAX_SIZE_MB:
        await message.reply_text(f"❌ فایل بزرگتر از {MAX_SIZE_MB}MB هست.")
        return
    try:
        ensure_free_space(file_size_mb)
    except DownloadUserError as e:
        await message.reply_text(f"❌ {e}")
        return

    size_str = f"{file_size_mb:.1f}MB" if file_size_mb >= 1 else f"{int(file_size_mb*1024)}KB"

    if uid not in _sessions:
        _sessions[uid] = {
            "files": [], "waiting": False,
            "collecting_forwards": True,
            "pending_forwards": [message],
        }
        _cancel_flags[uid] = False
        _forward_timers[uid] = asyncio.create_task(_forward_task(uid, context))
        return

    # حالت «فایل دیگه دارم» (waiting=True)
    status_msg = await message.reply_text(
        f"⬇️ در حال دانلود...\n📁 {short_name(original_filename)} ({size_str})",
        reply_markup=cancel_keyboard(uid)
    )
    _cancel_flags[uid] = False

    with busy(uid):
        try:
            save_path = await download_file_with_progress(
                message, original_filename, context, status_msg, uid
            )
        except asyncio.CancelledError:
            await status_msg.edit_text("❌ لغو شد.")
            cleanup_session(uid)
            return
        except Exception as e:
            await react(message, "❌", big=False)
            await status_msg.edit_text(safe_error_text("❌ خطا در دانلود.", e))
            return

    session = _sessions.get(uid)
    if not session:               # وسط دانلود /cancel زده شده
        cleanup(save_path)
        return

    await react(message, "✅", big=False)
    session["files"].append({"path": save_path, "name": original_filename})
    session["waiting"] = False
    _persist_sessions()

    files = session["files"]
    names, total_size, _ = files_summary(files)
    await status_msg.edit_text(
        f"✅ دانلود شد ({size_str})\n\n"
        f"📦 فایل‌های آماده ({len(files)} فایل — {total_size}):\n{names}\n\nفایل دیگه‌ای؟",
        reply_markup=more_keyboard(uid)
    )


async def _sleep_with_cancel(uid, seconds):
    for _ in range(int(seconds)):
        if _cancel_flags.get(uid):
            raise asyncio.CancelledError()
        await asyncio.sleep(1)


async def _send_to_rubika(uid, path, caption):
    """
    یک فایل رو با یک کلاینت تازه (برای هر تلاش) به «Saved Messages» روبیکا می‌فرسته.
    کلاینت تازه یعنی اگه اتصال قبلی خراب شده باشه، تلاش مجدد واقعاً از نو وصل می‌شه.
    آپلود به‌صورت تسک جدا اجرا می‌شه و هر ثانیه فلگر لغو چک می‌شه تا /cancel
    وسط آپلود هم کار کنه.
    """
    async def _upload():
        async with Client(name=RUBIKA_SESSION) as client:
            await client.send_document('me', path, caption=caption)

    task = asyncio.create_task(_upload())
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=1)
            if done:
                task.result()      # اگه آپلود خطا داشته، همین‌جا پرتاب می‌شه
                return
            if _cancel_flags.get(uid):
                raise asyncio.CancelledError()
    finally:
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


def _item_path(item):
    return item["path"] if isinstance(item, dict) else item


def _item_name(item):
    return item["name"] if isinstance(item, dict) else os.path.basename(item)


async def do_send(uid, safe_mode, query, files, total_size, zip_only=False):
    """
    صف ارسال: session["files"] (حالت معمولی) یا session["part_files"] (آرشیو 7z).
    هر آیتمی که با موفقیت ارسال بشه از صف حذف می‌شه؛ پس بعد از خطا یا ری‌استارت
    فقط همون چیزی که مونده ارسال می‌شه (بدون وابستگی به ایندکس).
    """
    session = _sessions[uid]
    if session.get("sending"):
        return                      # کلیک دوباره روی دکمه → ارسال دوم شروع نشه
    session["sending"] = True
    _cancel_flags[uid] = False

    # اگه آرشیو نیمه‌کاره‌ای از قبل هست، همون حالت (رمز/بی‌رمز) رو ادامه بده
    if session.get("part_files"):
        safe_mode = bool(session.get("safe_mode"))
        zip_only  = bool(session.get("zip_only"))
    session["safe_mode"] = safe_mode
    session["zip_only"]  = zip_only

    # منبع رمز، فقط لحظه‌ی ساخت آرشیو تعیین می‌شه و توی session می‌مونه:
    #   • ARCHIVE_PASSWORD تنظیم باشه → رمز ثابت؛ نه توی session ذخیره می‌شه نه توی کپشن میاد
    #   • وگرنه → رمز رندوم که توی session (برای ادامه بعد از ری‌استارت) و کپشن هست
    # اگه آرشیو از قبل ساخته شده، همون روشی که ساخته شده حفظ می‌شه.
    if safe_mode and not session.get("part_files"):
        session["pw_fixed"] = bool(ARCHIVE_PASSWORD)
        if session["pw_fixed"]:
            session.pop("password", None)
        elif not session.get("password"):
            session["password"] = generate_password()
    if not session.get("caption"):
        session["caption"] = get_shamsi()
    session.setdefault("sent", 0)
    session.setdefault("retries", {})

    pw_fixed = bool(session.get("pw_fixed"))
    password = (ARCHIVE_PASSWORD if pw_fixed else session.get("password")) if safe_mode else None
    caption  = session["caption"]
    if safe_mode:
        if pw_fixed:
            caption = f"{caption}\n🔐 آرشیو رمزدار (رمز ثابت خودت)"
        else:
            caption = f"{caption}\n🔑 رمز فایل: {password}"
        mode_label = "🛡 Safe+رمز"
    elif zip_only:
        mode_label = "🗜 زیپ"
    else:
        mode_label = "📎 معمولی"

    archive_mode = safe_mode or zip_only
    queue_key    = "part_files" if archive_mode else "files"
    unit         = "پارت" if archive_mode else "فایل"

    async def progress(text, show_cancel=True):
        if _cancel_flags.get(uid):
            raise asyncio.CancelledError()
        try:
            await query.edit_message_text(
                f"{mode_label} | {total_size}\n\n{text}",
                reply_markup=cancel_keyboard(uid) if show_cancel else None
            )
        except Exception:
            pass

    def queue_status():
        queue = session.get(queue_key) or []
        lines = []
        if session["sent"]:
            lines.append(f"  ✅ {session['sent']} {unit} ارسال شد")
        for n, item in enumerate(queue[:8], session["sent"] + 1):
            p = _item_path(item)
            sz = os.path.getsize(p) / (1024*1024) if os.path.exists(p) else 0
            r = session["retries"].get(p, 0)
            retry_txt = f" (تلاش {r})" if r > 0 else ""
            lines.append(
                f"  📤 {unit} {n} ({sz:.1f}MB) ← ارسال{retry_txt}" if n == session["sent"] + 1
                else f"  ⏳ {unit} {n} ({sz:.1f}MB)"
            )
        if len(queue) > 8:
            lines.append(f"  … و {len(queue) - 8} مورد دیگه")
        return "\n".join(lines)

    try:
        with busy(uid):
            # ── ساخت آرشیو (فقط اگه هنوز ساخته نشده) ──
            if archive_mode and not session.get("part_files"):
                paths = [f["path"] for f in session["files"] if os.path.exists(f["path"])]
                if not paths:
                    raise Exception("هیچ فایلی برای آرشیو باقی نمونده.")
                total_mb = sum(os.path.getsize(p) for p in paths) / (1024*1024)
                ensure_free_space(total_mb * 1.05)   # آرشیو موقتاً هم‌اندازه‌ی فایل‌هاست
                est_parts = max(1, math.ceil(total_mb / PART_SIZE_MB)) if total_mb >= SPLIT_THRESHOLD_MB else 1

                await progress(
                    f"{'🔐 ساخت 7z رمزدار AES-256' if safe_mode else '🗜 ساخت 7z'}...\n"
                    f"📦 تخمین: {est_parts} پارت\n"
                    f"💾 حجم کل: {total_mb:.1f}MB"
                )

                base_zip = os.path.join(DOWNLOAD_DIR, f"bundle_{uid}")
                loop = asyncio.get_running_loop()
                part_files = await loop.run_in_executor(
                    None,
                    lambda: create_7z(
                        base_zip, password,
                        PART_SIZE_MB if total_mb >= SPLIT_THRESHOLD_MB else 0,
                        *paths
                    )
                )
                log_archive(total_mb, len(part_files))

                # اول وضعیت جدید روی دیسک ثبت می‌شه، بعد فایل‌های اصلی پاک می‌شن؛
                # اگه همین بین کرش کنیم هیچ چیزی گم نمی‌شه.
                session["part_files"] = part_files
                session["files"] = []
                session["sent"] = 0
                _persist_sessions()
                for p in paths:
                    cleanup(p)

                await progress(
                    f"✅ آرشیو آماده شد!\n📦 {len(part_files)} پارت ساخته شد\n📤 شروع ارسال..."
                )
                await asyncio.sleep(1)

            total_items = session["sent"] + len(session.get(queue_key) or [])

            # ── حلقه‌ی ارسال ──
            while session.get(queue_key):
                if _cancel_flags.get(uid):
                    raise asyncio.CancelledError()

                item = session[queue_key][0]
                path = _item_path(item)

                if not os.path.exists(path):
                    # فایل از دیسک رفته؛ تلاش مجدد بی‌فایده‌ست، ردش کن
                    logger.warning("فایل %s روی دیسک پیدا نشد؛ از صف حذف شد.", path)
                    session[queue_key].pop(0)
                    _persist_sessions()
                    continue

                idx     = session["sent"] + 1
                size_mb = os.path.getsize(path) / (1024*1024)
                part_caption = (
                    f"{caption}\n📦 پارت {idx}/{total_items}"
                    if archive_mode and total_items > 1 else caption
                )

                await progress(f"📦 وضعیت:\n{queue_status()}")

                try:
                    await _send_to_rubika(uid, path, part_caption)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    retry_count = session["retries"].get(path, 0) + 1
                    session["retries"][path] = retry_count
                    if retry_count >= MAX_AUTO_RETRY:
                        # صفر می‌کنیم تا اگه کاربر دوباره دکمه رو زد از نو retry بشه
                        session["retries"][path] = 0
                        raise Exception(f"{unit} {idx} بعد از {retry_count} بار تلاش ناموفق بود:\n{e}")
                    logger.warning("ارسال %s %s ناموفق (تلاش %s): %s", unit, idx, retry_count, e)
                    await progress(
                        f"📦 وضعیت:\n{queue_status()}\n\n"
                        f"⚠️ خطا در {unit} {idx} — تلاش {retry_count}/{MAX_AUTO_RETRY}\n"
                        f"🔄 تلاش مجدد در 30s..."
                    )
                    await _sleep_with_cancel(uid, 30)
                    continue

                # موفق: از صف حذف + ثبت روی دیسک (بعد از ری‌استارت دوباره فرستاده نمی‌شه)
                log_send(size_mb)
                session[queue_key].pop(0)
                session["sent"] += 1
                session["retries"].pop(path, None)
                cleanup(path)
                _persist_sessions()

                if session[queue_key]:
                    nxt = _item_path(session[queue_key][0])
                    next_size = os.path.getsize(nxt) / (1024*1024) if os.path.exists(nxt) else 50
                    delay = smart_delay(next_size)
                    for remaining_sec in range(delay, 0, -5):
                        if _cancel_flags.get(uid):
                            raise asyncio.CancelledError()
                        await progress(
                            f"📦 وضعیت:\n{queue_status()}\n\n"
                            f"⏳ {remaining_sec}s تا {unit} بعدی..."
                        )
                        await asyncio.sleep(min(5, remaining_sec))

            sent_total = session["sent"]
            _sessions.pop(uid, None)
            _cancel_flags.pop(uid, None)
            _persist_sessions()
            if archive_mode:
                await query.edit_message_text(
                    f"✅ همه {'پارت‌ها' if sent_total > 1 else 'فایل'} ارسال شدن!\n📦 {sent_total} پارت"
                )
            else:
                await query.edit_message_text(f"✅ {sent_total} فایل به روبیکا ارسال شد!")

    except asyncio.CancelledError:
        cleanup_session(uid)
        try:
            await query.edit_message_text("❌ لغو شد. همه فایل‌ها از سرور پاک شدن.")
        except Exception:
            pass

    except Exception as e:
        logger.exception("خطا در فرآیند ارسال فایل به روبیکا")
        remaining = len(session.get(queue_key) or [])
        try:
            await query.edit_message_text(
                safe_error_text("❌ ارسال با خطا مواجه شد.", e) + "\n\n"
                f"✅ {session['sent']} {unit} ارسال شده، {remaining} {unit} باقی مونده\n\n"
                f"برای ادامه دوباره دکمه ارسال رو بزن:",
                reply_markup=send_keyboard(uid)
            )
        except Exception:
            pass

    finally:
        session["sending"] = False


_URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed(update.message.from_user.id):
        await deny_access(update)
        return
    text = update.message.text.strip()
    uid  = str(update.message.from_user.id)

    if text == "🔗 دانلود از لینک":
        await ask_for_link(update, context)
        return
    if text == "📊 وضعیت ربات":
        await bot_status(update, context)
        return

    # لینک رو از هر جای متن پیدا می‌کنیم؛ حتی بدون زدن دکمه‌ی «دانلود از لینک»
    m = _URL_RE.search(text)
    if m:
        _link_sessions.pop(uid, None)
        await handle_link_download(uid, m.group(0), update.message, context)
        return

    if _link_sessions.get(uid, {}).get("waiting"):
        await update.message.reply_text(
            "⚠️ لینک معتبر نیست. با http/https شروع بشه (یا /cancel).",
            reply_markup=MAIN_REPLY_KEYBOARD,
        )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if not is_allowed(query.from_user.id):
        try:
            await query.edit_message_text("❌ دسترسی ندارید.")
        except Exception:
            pass
        return

    data  = query.data

    if data.startswith("ytdlp:"):
        _, uid, format_id = data.split(":", 2)
        await handle_ytdlp_quality_callback(uid, format_id, query)
        return
    if data.startswith("cancel_ytdlp:"):
        uid = data.split(":", 1)[1]
        _cancel_flags[f"link:{uid}"] = True   # دانلود در حال اجرا با دیدن فلگ متوقف می‌شه
        _link_sessions.pop(uid, None)
        try:
            await query.edit_message_text("❌ لغو شد.")
        except Exception:
            pass
        return

    action, uid = data.split(":", 1)
    session     = _sessions.get(uid)

    if action == "cancel":
        if is_busy(uid):
            _cancel_flags[uid] = True      # عملیات در حال اجرا خودش پاک‌سازی می‌کنه
            await asyncio.sleep(1.5)
        if uid in _sessions:
            cleanup_session(uid)
        try:
            await query.edit_message_text("❌ لغو شد. همه فایل‌ها از سرور پاک شدن.")
        except Exception:
            pass
        return

    if not session:
        await query.edit_message_text("❌ session منقضی شده، دوباره فایل بفرست.")
        return

    files = session["files"]
    names, total_size, total_mb = files_summary(files)
    parts_pending = session.get("part_files") or []

    if parts_pending and action in ("more", "done"):
        # آرشیو از قبل ساخته شده (مثلاً بعد از ری‌استارت)؛ فقط ارسالِ ادامه معنی داره
        await query.edit_message_text(
            f"📦 آرشیو آماده‌ی ارسال: {len(parts_pending)} پارت باقی مونده.\n\nادامه بدم؟",
            reply_markup=send_keyboard(uid)
        )
        return

    if action == "more":
        _sessions[uid]["waiting"] = True
        _persist_sessions()
        await query.edit_message_text(
            f"👌 فایل بعدی رو بفرست.\n\n"
            f"📦 دانلود شده ({len(files)} فایل — {total_size}):\n{names}"
        )
        return

    if action == "done":
        split_note = ""
        if total_mb >= SPLIT_THRESHOLD_MB:
            parts = math.ceil(total_mb / PART_SIZE_MB)
            split_note = f"\n⚠️ Safe Mode: ~{parts} پارت {PART_SIZE_MB}MB"
        await query.edit_message_text(
            f"📦 {len(files)} فایل آماده ({total_size}):\n{names}{split_note}\n\nچطور ارسال کنم؟",
            reply_markup=send_keyboard(uid)
        )
        return

    if action in ("send_normal", "send_safe", "send_zip"):
        safe_mode = (action == "send_safe")
        zip_only  = (action == "send_zip")
        await do_send(uid, safe_mode, query, files, total_size, zip_only=zip_only)
        return


async def _on_startup(app):
    """
    بعد از initialize شدن اپ و قبل از شروع polling صدا زده می‌شه (الگوی
    رسمی post_init در python-telegram-bot). session‌های ذخیره‌شده از اجرای
    قبلی رو بازیابی می‌کنه (فقط اونایی که فایل‌هاشون هنوز واقعاً روی دیسک
    هستن) و اگه چیزی برای بازیابی بود، به کاربر مجاز اطلاع می‌ده که کار
    قبلیش از دست نرفته.
    """
    recovered = session_store.recover_sessions()
    if not recovered:
        return
    _sessions.update(recovered)
    logger.info(f"{len(recovered)} session از اجرای قبلی بازیابی شد.")
    if not ALLOWED_USER_ID:
        return
    try:
        for uid, session in recovered.items():
            files = session.get("files", [])
            parts = session.get("part_files", [])
            names, total_size, _ = files_summary(files) if files else ("", "۰", 0)
            what = (f"{len(parts)} پارت آرشیو" if parts else f"{len(files)} فایل ({total_size})")
            await app.bot.send_message(
                chat_id=int(uid),
                text=(
                    "🔄 ربات ری‌استارت شد.\n\n"
                    f"یک session ناتموم پیدا شد با {what} که هنوز روی دیسک سالم مونده.\n"
                    "با دکمه‌ی زیر ارسال رو ادامه بده؛ چیزهایی که قبلاً با موفقیت "
                    "به روبیکا رفته بودن دوباره فرستاده نمی‌شن."
                ),
                reply_markup=send_keyboard(uid),
            )
    except Exception:
        logger.exception("اطلاع‌رسانی بازیابی session به کاربر ناموفق بود")


async def _on_error(update, context):
    logger.error("خطای مدیریت‌نشده در هندلر", exc_info=context.error)


def run_telegram_bot():
    # concurrent_updates=True ضروریه: بدون اون PTB آپدیت‌ها رو یکی‌یکی پردازش می‌کنه و
    # وقتی دانلود/ارسال داخل یک هندلر await شده، کلیک روی «لغو» تا پایان همون
    # عملیات پردازش نمی‌شد.
    app = (
        ApplicationBuilder()
        .token(TELEGRAM_TOKEN)
        .concurrent_updates(True)
        .post_init(_on_startup)
        .build()
    )
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("update", cmd_update))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO |
        filters.PHOTO | filters.VOICE,
        handle_file
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(_on_error)
    logger.info("🤖 ربات تلگرام شروع به کار کرد...")
    app.run_polling()


