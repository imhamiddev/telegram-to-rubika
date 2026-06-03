import os
import re
import glob
import math
import asyncio
import time
import subprocess
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, KeyboardButton
from telegram.ext import (
    ApplicationBuilder, MessageHandler, CallbackQueryHandler,
    filters, ContextTypes, CommandHandler
)
from config import TELEGRAM_TOKEN, DOWNLOAD_DIR, MAX_SIZE_MB, FIXED_PASSWORD, ALLOWED_USER_ID
from rubika_bot import random_filename, cleanup, PART_SIZE_MB
from rubpy import Client
import jdatetime
from downloader import (
    download_direct_link,
    download_video_from_page,
    get_video_info,
    is_direct_link,
)
from stats import log_download, log_send, log_archive, format_stats_text  # ← آمار

RUBIKA_SESSION          = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'my_session')
LARGE_FILE_THRESHOLD_MB = 20
SPLIT_THRESHOLD_MB      = 100
FORWARD_COLLECT_SECONDS = 5
MAX_AUTO_RETRY          = 15

_sessions       = {}
_forward_timers = {}
_link_sessions  = {}
_cancel_flags   = {}


def smart_delay(file_size_mb: float) -> int:
    if file_size_mb < 10:    return 10
    elif file_size_mb < 50:  return 30
    elif file_size_mb < 100: return 60
    else:                    return 120


MAIN_REPLY_KEYBOARD = ReplyKeyboardMarkup(
    [[
        KeyboardButton("🔗 دانلود از لینک", api_kwargs={"style": "primary"}),
        KeyboardButton("📊 وضعیت ربات",     api_kwargs={"style": "primary"}),
    ]],
    resize_keyboard=True,
)

def cancel_keyboard(uid):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("❌ لغو و حذف همه", callback_data=f"cancel:{uid}", style="danger")]
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


SEVENZIP_BIN = os.path.expanduser("~/.local/bin/7za")

def create_7z(output_base, password, part_size_mb, *file_paths):
    existing = glob.glob(output_base + ".7z*")
    for f in existing:
        try: os.remove(f)
        except: pass

    cmd = [SEVENZIP_BIN, "a", "-mx=0", "-bsp0", "-bso0"]

    if password:
        cmd.append(f"-p{password}")

    if part_size_mb:
        cmd.append(f"-v{part_size_mb}m")

    cmd.append(output_base + ".7z")
    cmd.extend(fp for fp in file_paths if os.path.exists(fp))

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode not in (0, 1):
        raise Exception(f"خطا در 7za: {result.stderr or result.stdout}")

    parts = sorted(glob.glob(output_base + ".7z.*"))
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
    first = update.message.from_user.first_name or "کاربر"
    await update.message.reply_text(
        f"👋 سلام {first}!\n\n"
        "به ربات انتقال فایل خوش اومدی 🚀\n\n"
        "📌 فایلت رو بفرست تا دانلود و به روبیکا ارسال بشه.\n"
        "⬇️ از دکمه‌های پایین هم می‌تونی استفاده کنی:",
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("ساخته شده توسط حمید", url="https://t.me/imhamiddev", style="success")]
        ])
    )


async def bot_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid    = str(update.message.from_user.id)
    active = len(_sessions)
    disk_files = glob.glob(os.path.join(DOWNLOAD_DIR, "*"))
    disk_mb = sum(os.path.getsize(f) for f in disk_files if os.path.isfile(f)) / (1024*1024)
    session_info = ""
    if uid in _sessions:
        files = _sessions[uid].get("files", [])
        _, sz, _ = files_summary(files) if files else ("", "0MB", 0)
        session_info = f"\n\n🗂 جلسه فعال شما: {len(files)} فایل ({sz})"

    # ← آمار از دیتابیس
    stats_text = format_stats_text()

    await update.message.reply_text(
        f"🟢 ربات آنلاین\n"
        f"👥 جلسه‌های فعال: {active}\n"
        f"💾 فضای موقت: {disk_mb:.1f} MB"
        f"{session_info}\n\n"
        f"{stats_text}",
        reply_markup=MAIN_REPLY_KEYBOARD,
    )


async def ask_for_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = str(update.message.from_user.id)
    _link_sessions[uid] = {"waiting": True}
    await update.message.reply_text(
        "🔗 لینک رو بفرست:\n\n"
        "• لینک مستقیم فایل\n"
        "• یا آدرس صفحه‌ای که ویدیو داخلشه\n\n"
        "❌ برای لغو /cancel بزن",
        reply_markup=MAIN_REPLY_KEYBOARD,
    )

async def handle_link_download(uid, url, message, context):
    status_msg = await message.reply_text(f"🔍 در حال بررسی لینک...\n🔗 {url[:60]}")

    if is_direct_link(url):
        await status_msg.edit_text(f"⬇️ دانلود لینک مستقیم...\n🔗 {url[:60]}")
        try:
            save_path = await download_direct_link(uid, url)
        except Exception as e:
            await status_msg.edit_text(f"❌ خطا:\n{e}")
            return
        filename = os.path.basename(save_path)
        file_mb  = os.path.getsize(save_path) / (1024*1024)
        log_download(file_mb)  # ← ثبت آمار
        if uid not in _sessions:
            _sessions[uid] = {"files": [], "waiting": False}
        _sessions[uid]["files"].append({"path": save_path, "name": filename})
        files = _sessions[uid]["files"]
        names, total_size, _ = files_summary(files)
        await status_msg.edit_text(
            f"✅ دانلود شد ({file_mb:.1f}MB)\n\n"
            f"📦 فایل‌های آماده ({len(files)} — {total_size}):\n{names}\n\nفایل دیگه‌ای؟",
            reply_markup=more_keyboard(uid),
        )
        return

    await status_msg.edit_text("🔍 در حال پیدا کردن ویدیو...")
    try:
        info = await get_video_info(url)
    except Exception as e:
        await status_msg.edit_text(f"❌ ویدیویی پیدا نشد:\n{e}")
        return

    formats  = info["formats"]
    title    = info["title"][:40]
    duration = info["duration"]
    _link_sessions[uid] = {
        "waiting": False, "ytdlp_url": url,
        "ytdlp_formats": formats, "ytdlp_title": info["title"],
        "status_msg_id": status_msg.message_id,
    }
    buttons = []
    for f in formats:
        size_text = f" ({f['filesize_mb']}MB)" if f['filesize_mb'] > 0 else ""
        style = "primary" if f.get("type") == "audio" else "success"
        buttons.append([InlineKeyboardButton(
            f"{f['label']}{size_text}", callback_data=f"ytdlp:{uid}:{f['format_id']}",
            style=style
        )])
    buttons.append([InlineKeyboardButton("❌ لغو", callback_data=f"cancel_ytdlp:{uid}", style="danger")])
    await status_msg.edit_text(
        f"🎬 {title}\n⏱ {duration}\n\nکیفیت:",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

async def handle_ytdlp_quality_callback(uid, format_id, query):
    session = _link_sessions.get(uid, {})
    url     = session.get("ytdlp_url")
    title   = session.get("ytdlp_title", "video")
    if not url:
        await query.edit_message_text("❌ session منقضی.")
        return
    async def progress_update(text):
        try: await query.edit_message_text(f"⬇️ دانلود ویدیو...\n{text}")
        except Exception: pass
    await query.edit_message_text("⬇️ دانلود شروع شد...")
    try:
        audio_only = format_id.startswith("bestaudio") or "kbps" in session.get("ytdlp_formats", [{}])[0].get("label", "")
        # پیدا کردن نوع فرمت انتخاب‌شده
        selected = next((f for f in session.get("ytdlp_formats", []) if f["format_id"] == format_id), {})
        audio_only = selected.get("type") == "audio"
        save_path = await download_video_from_page(uid, url, format_id, progress_update, audio_only=audio_only)
    except Exception as e:
        await query.edit_message_text(f"❌ خطا:\n{e}")
        _link_sessions.pop(uid, None)
        return
    _link_sessions.pop(uid, None)
    file_mb   = os.path.getsize(save_path) / (1024*1024)
    real_name = os.path.basename(save_path)
    log_download(file_mb)  # ← ثبت آمار
    if uid not in _sessions:
        _sessions[uid] = {"files": [], "waiting": False}
    _sessions[uid]["files"].append({"path": save_path, "name": real_name})
    files = _sessions[uid]["files"]
    names, total_size, _ = files_summary(files)
    await query.edit_message_text(
        f"✅ ویدیو دانلود شد ({file_mb:.1f}MB)\n\n"
        f"📦 ({len(files)} فایل — {total_size}):\n{names}\n\nفایل دیگه‌ای؟",
        reply_markup=more_keyboard(uid),
    )


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

    if file_size_mb > LARGE_FILE_THRESHOLD_MB:
        from tg_client import download_large_file
        async def dl_progress(text):
            if _cancel_flags.get(uid):
                raise asyncio.CancelledError()
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
        result = await download_large_file(message, original_filename, dl_progress)
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

    prog_task = asyncio.get_event_loop().create_task(live_progress())
    try:
        await tg_file.download_to_drive(save_path)
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


async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    uid     = str(message.from_user.id)

    if ALLOWED_USER_ID and message.from_user.id != ALLOWED_USER_ID:
        await message.reply_text("❌ دسترسی ندارید.")
        return

    file, original_filename = get_file_info(message)
    if not file:
        await message.reply_text("❌ فرمت پشتیبانی نمیشه.")
        return

    await react(message, "👀", big=True)

    if uid in _sessions and not _sessions[uid].get("waiting", False):
        if _sessions[uid].get("collecting_forwards"):
            _sessions[uid]["pending_forwards"].append(message)
            return
        await message.reply_text("⚠️ اول روی دکمه‌های پیام قبلی کلیک کن.")
        return

    file_size_mb = (file.file_size or 0) / (1024*1024)
    if file_size_mb > MAX_SIZE_MB:
        await message.reply_text(f"❌ فایل بزرگتر از {MAX_SIZE_MB}MB هست.")
        return

    size_str = f"{file_size_mb:.1f}MB" if file_size_mb >= 1 else f"{int(file_size_mb*1024)}KB"

    if uid not in _sessions:
        _sessions[uid] = {
            "files": [], "waiting": False,
            "collecting_forwards": True,
            "pending_forwards": [message],
        }
        _cancel_flags[uid] = False

        async def process_forwards():
            await asyncio.sleep(FORWARD_COLLECT_SECONDS)
            if uid not in _sessions:
                return

            pending = _sessions[uid].pop("pending_forwards", [])
            _sessions[uid]["collecting_forwards"] = False

            status_msg = await pending[0].reply_text(
                f"⬇️ در حال دانلود {len(pending)} فایل...",
                reply_markup=cancel_keyboard(uid)
            )

            downloaded_so_far = []

            for i, msg in enumerate(pending, 1):
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
                        _sessions[uid]["files"].append(entry)
                        downloaded_so_far.append(entry)
                        await react(msg, "✅", big=False)
                except asyncio.CancelledError:
                    await status_msg.edit_text("❌ لغو شد. فایل‌های دانلود‌شده پاک شدن.")
                    cleanup_session(uid)
                    return
                except Exception as e:
                    await react(msg, "❌", big=False)
                    await status_msg.edit_text(f"❌ خطا در دانلود {short_name(fname)}: {e}")
                    cleanup_session(uid)
                    return

            files = _sessions[uid]["files"]
            if not files:
                del _sessions[uid]
                return

            names, total_size, _ = files_summary(files)
            await status_msg.edit_text(
                f"✅ {len(files)} فایل دانلود شد ({total_size})\n\n"
                f"📦 فایل‌ها:\n{names}\n\nفایل دیگه‌ای هم داری؟",
                reply_markup=more_keyboard(uid)
            )

        task = asyncio.get_event_loop().create_task(process_forwards())
        _forward_timers[uid] = task
        return

    status_msg = await message.reply_text(
        f"⬇️ در حال دانلود...\n📁 {short_name(original_filename)} ({size_str})",
        reply_markup=cancel_keyboard(uid)
    )
    _cancel_flags[uid] = False

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
        await status_msg.edit_text(f"❌ خطا در دانلود: {e}")
        return

    await react(message, "✅", big=False)
    _sessions[uid]["files"].append({"path": save_path, "name": original_filename})
    _sessions[uid]["waiting"] = False

    files = _sessions[uid]["files"]
    names, total_size, _ = files_summary(files)
    await status_msg.edit_text(
        f"✅ دانلود شد ({size_str})\n\n"
        f"📦 فایل‌های آماده ({len(files)} فایل — {total_size}):\n{names}\n\nفایل دیگه‌ای؟",
        reply_markup=more_keyboard(uid)
    )


async def do_send(uid, safe_mode, query, files, total_size, zip_only=False):
    all_paths = [f["path"] for f in files]
    session   = _sessions[uid]
    _cancel_flags[uid] = False

    if "password"   not in session: session["password"]   = FIXED_PASSWORD if safe_mode else None
    if "caption"    not in session: session["caption"]    = get_shamsi()
    if "start_from" not in session: session["start_from"] = 0
    if "retries"    not in session: session["retries"]    = {}

    password   = session["password"]
    caption    = session["caption"]
    if safe_mode:   mode_label = "🛡 Safe+رمز"
    elif zip_only:  mode_label = "🗜 زیپ"
    else:           mode_label = "📎 معمولی"

    async def progress(text, show_cancel=True):
        if _cancel_flags.get(uid):
            raise asyncio.CancelledError()
        try:
            kb = cancel_keyboard(uid) if show_cancel else None
            await query.edit_message_text(
                f"{mode_label} | {len(files)} فایل ({total_size})\n\n{text}",
                reply_markup=kb
            )
        except Exception:
            pass

    try:
        if safe_mode or zip_only:
            base_zip       = os.path.join(DOWNLOAD_DIR, f"bundle_{uid}")
            existing_parts = session.get("part_files")
            remaining      = [p for p in existing_parts if os.path.exists(p)] if existing_parts else []

            if existing_parts and remaining:
                part_files = existing_parts
            else:
                total_bytes = sum(os.path.getsize(p) for p in all_paths if os.path.exists(p))
                total_mb    = total_bytes / (1024*1024)
                est_parts   = max(1, math.ceil(total_mb / PART_SIZE_MB)) if total_mb >= SPLIT_THRESHOLD_MB else 1

                await progress(
                    f"{'🔐 ساخت 7z رمزدار AES-256' if safe_mode else '🗜 ساخت 7z'}...\n"
                    f"📦 تخمین: {est_parts} پارت\n"
                    f"💾 حجم کل: {total_mb:.1f}MB"
                )

                loop = asyncio.get_event_loop()
                part_files = await loop.run_in_executor(
                    None,
                    lambda: create_7z(
                        base_zip,
                        password if safe_mode else None,
                        PART_SIZE_MB if total_mb >= SPLIT_THRESHOLD_MB else 0,
                        *all_paths
                    )
                )

                # ← ثبت آمار آرشیو
                log_archive(total_mb, len(part_files))

                session["part_files"] = part_files
                for p in all_paths:
                    cleanup(p)

                await progress(
                    f"✅ آرشیو آماده شد!\n"
                    f"📦 {len(part_files)} پارت ساخته شد\n"
                    f"📤 شروع ارسال..."
                )
                await asyncio.sleep(1)

            total_parts = len(part_files)

            def parts_status(current_i):
                lines = []
                for idx, pf in enumerate(part_files, 1):
                    psize   = os.path.getsize(pf)/(1024*1024) if os.path.exists(pf) else 0
                    retries = session["retries"].get(idx, 0)
                    retry_txt = f" (تلاش {retries})" if retries > 0 else ""
                    if idx < current_i:
                        lines.append(f"  ✅ پارت {idx} ({psize:.1f}MB){retry_txt}")
                    elif idx == current_i:
                        lines.append(f"  📤 پارت {idx} ({psize:.1f}MB) ← ارسال{retry_txt}")
                    else:
                        lines.append(f"  ⏳ پارت {idx} ({psize:.1f}MB)")
                return "\n".join(lines)

            async with Client(name=RUBIKA_SESSION) as client:
                i = session.get("start_from", 0) + 1
                while i <= total_parts:
                    if _cancel_flags.get(uid):
                        raise asyncio.CancelledError()

                    part_path    = part_files[i - 1]
                    part_size_mb = os.path.getsize(part_path)/(1024*1024) if os.path.exists(part_path) else 0
                    part_caption = f"{caption}\n📦 پارت {i}/{total_parts}" if total_parts > 1 else caption

                    await progress(f"📦 وضعیت:\n{parts_status(i)}")

                    retry_count = session["retries"].get(i, 0)
                    try:
                        await client.send_document('me', part_path, caption=part_caption)
                        log_send(part_size_mb)  # ← ثبت آمار
                        session["start_from"] = i
                        session["retries"][i] = 0
                        cleanup(part_path)
                        i += 1

                        if i <= total_parts:
                            next_size = os.path.getsize(part_files[i-1])/(1024*1024) if os.path.exists(part_files[i-1]) else 50
                            delay = smart_delay(next_size)
                            for remaining_sec in range(delay, 0, -5):
                                if _cancel_flags.get(uid):
                                    raise asyncio.CancelledError()
                                await progress(
                                    f"📦 وضعیت:\n{parts_status(i)}\n\n"
                                    f"⏳ {remaining_sec}s تا پارت بعدی..."
                                )
                                await asyncio.sleep(min(5, remaining_sec))

                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        retry_count += 1
                        session["retries"][i] = retry_count
                        if retry_count >= MAX_AUTO_RETRY:
                            raise Exception(
                                f"پارت {i} بعد از {retry_count} بار تلاش ناموفق بود:\n{e}"
                            )
                        wait_sec = 30
                        await progress(
                            f"📦 وضعیت:\n{parts_status(i)}\n\n"
                            f"⚠️ خطا در پارت {i} — تلاش {retry_count}/{MAX_AUTO_RETRY}\n"
                            f"🔄 تلاش مجدد در {wait_sec}s..."
                        )
                        await asyncio.sleep(wait_sec)

        else:
            async with Client(name=RUBIKA_SESSION) as client:
                i = session.get("start_from", 0) + 1
                while i <= len(files):
                    if _cancel_flags.get(uid):
                        raise asyncio.CancelledError()

                    path = all_paths[i-1]
                    name = files[i-1]["name"]
                    file_mb = os.path.getsize(path)/(1024*1024) if os.path.exists(path) else 0
                    await progress(f"📤 ارسال {i}/{len(files)}\n📁 {short_name(name)}")

                    retry_count = session["retries"].get(i, 0)
                    try:
                        await client.send_document('me', path, caption=caption)
                        log_send(file_mb)  # ← ثبت آمار
                        session["start_from"] = i
                        session["retries"][i] = 0
                        cleanup(path)
                        i += 1

                        if i <= len(files):
                            delay = smart_delay(file_mb)
                            for remaining_sec in range(delay, 0, -5):
                                if _cancel_flags.get(uid):
                                    raise asyncio.CancelledError()
                                await progress(
                                    f"✅ فایل {i-1} ارسال شد\n"
                                    f"⏳ {remaining_sec}s تا فایل بعدی..."
                                )
                                await asyncio.sleep(min(5, remaining_sec))

                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        retry_count += 1
                        session["retries"][i] = retry_count
                        if retry_count >= MAX_AUTO_RETRY:
                            raise Exception(
                                f"فایل {i} بعد از {retry_count} بار تلاش ناموفق:\n{e}"
                            )
                        wait_sec = 30
                        await progress(
                            f"📤 ارسال {i}/{len(files)}\n"
                            f"⚠️ خطا — تلاش {retry_count}/{MAX_AUTO_RETRY}\n"
                            f"🔄 تلاش مجدد در {wait_sec}s..."
                        )
                        await asyncio.sleep(wait_sec)

        del _sessions[uid]
        _cancel_flags.pop(uid, None)
        if safe_mode or zip_only:
            parts_count = len(session.get("part_files", []))
            await query.edit_message_text(
                f"✅ همه {'پارت‌ها' if parts_count > 1 else 'فایل'} ارسال شدن!\n"
                f"📦 {parts_count} پارت"
            )
        else:
            await query.edit_message_text(f"✅ {len(files)} فایل به روبیکا ارسال شد!")

    except asyncio.CancelledError:
        cleanup_session(uid)
        try:
            await query.edit_message_text("❌ لغو شد. همه فایل‌ها از سرور پاک شدن.")
        except Exception:
            pass

    except Exception as e:
        sent  = session.get("start_from", 0)
        total = len(session.get("part_files", all_paths))
        try:
            await query.edit_message_text(
                f"❌ خطا:\n{e}\n\n"
                f"✅ {sent} از {total} {'پارت' if safe_mode else 'فایل'} ارسال شده\n\n"
                f"برای ادامه دوباره دکمه ارسال رو بزن:",
                reply_markup=send_keyboard(uid)
            )
        except Exception:
            pass


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    uid  = str(update.message.from_user.id)

    if text == "🔗 دانلود از لینک":
        await ask_for_link(update, context)
        return
    if text == "📊 وضعیت ربات":
        await bot_status(update, context)
        return
    if text == "/cancel":
        _link_sessions.pop(uid, None)
        await update.message.reply_text("❌ لغو شد.", reply_markup=MAIN_REPLY_KEYBOARD)
        return
    if _link_sessions.get(uid, {}).get("waiting"):
        _link_sessions.pop(uid, None)
        if text.startswith("http://") or text.startswith("https://"):
            await handle_link_download(uid, text, update.message, context)
        else:
            await update.message.reply_text(
                "⚠️ لینک معتبر نیست. با http/https شروع بشه.",
                reply_markup=MAIN_REPLY_KEYBOARD,
            )


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data  = query.data

    if data.startswith("ytdlp:"):
        _, uid, format_id = data.split(":", 2)
        await handle_ytdlp_quality_callback(uid, format_id, query)
        return
    if data.startswith("cancel_ytdlp:"):
        uid = data.split(":", 1)[1]
        _link_sessions.pop(uid, None)
        await query.edit_message_text("❌ لغو شد.")
        return

    action, uid = data.split(":", 1)
    session     = _sessions.get(uid)

    if action == "cancel":
        _cancel_flags[uid] = True
        await asyncio.sleep(0.5)
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

    if action == "more":
        _sessions[uid]["waiting"] = True
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
        session["safe_mode"] = safe_mode
        await do_send(uid, safe_mode, query, files, total_size, zip_only=zip_only)
        return


def run_telegram_bot():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(MessageHandler(
        filters.Document.ALL | filters.VIDEO | filters.AUDIO |
        filters.PHOTO | filters.VOICE,
        handle_file
    ))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("🤖 ربات تلگرام شروع به کار کرد...")
    app.run_polling()