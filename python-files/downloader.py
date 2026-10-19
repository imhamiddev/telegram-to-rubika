"""
downloader.py — ماژول دانلود برای ربات تلگرام

سه موتور دانلود:
  • wget       → لینک مستقیم فایل (zip, mp4, pdf, ...)
  • yt-dlp     → ویدیو/صوت (یوتیوب، اینستاگرام، توییتر/X، تیک‌تاک، ...)
  • gallery-dl → عکس/گالری/کاروسل (اینستاگرام، توییتر/X، پینترست، ردیت، ...)

الگوی انتخاب موتور (همون الگوی ابزارهای حرفه‌ای مثل Nami/smdl):
  yt-dlp برای ویدیو، و اگه لینک ویدیو نداشت (پست عکس‌دار) → gallery-dl.
"""

import os
import re
import sys
import json
import time
import glob
import shutil
import secrets
import asyncio
import logging
import importlib.util
from urllib.parse import urlparse

from config import (
    DOWNLOAD_DIR, MAX_SIZE_MB, MIN_FREE_MB,
    COOKIES_FILE, PROXY, JS_RUNTIME, GALLERY_MAX_FILES,
)

logger = logging.getLogger(__name__)

# ابزارها رو با خود پایتون فعلی (sys.executable -m ...) صدا می‌زنیم، نه از PATH؛
# این‌طوری اگه ربات با venv اجرا بشه (بدون activate) هم yt-dlp/gallery-dl پیدا می‌شن.
YTDLP_CMD = [sys.executable, "-m", "yt_dlp"]
GALLERY_CMD = [sys.executable, "-m", "gallery_dl"]

PROGRESS_INTERVAL = 3.0   # حداقل فاصله‌ی دو آپدیت پیشرفت (ثانیه) — جلوگیری از flood تلگرام

# ─── دسته‌بندی سایت‌ها ──────────────────────────────────────────

# سایت‌هایی که yt-dlp بهتره ازشون دانلود کنه (برای تشخیص «لینک مستقیم نیست»)
YTDLP_DOMAINS = {
    "youtube.com", "youtu.be",
    "instagram.com", "instagr.am",
    "twitter.com", "x.com",
    "tiktok.com", "vm.tiktok.com",
    "facebook.com", "fb.watch",
    "vimeo.com",
    "dailymotion.com",
    "twitch.tv", "clips.twitch.tv",
    "reddit.com", "v.redd.it",
    "pinterest.com",
    "soundcloud.com",
    "aparat.com",
    "telewebion.com",
}

# سایت‌هایی که gallery-dl ازشون عکس/گالری می‌گیره (fallback بعد از yt-dlp)
GALLERY_DOMAINS = {
    "instagram.com", "twitter.com", "x.com", "pinterest.com", "pin.it",
    "reddit.com", "tumblr.com", "pixiv.net", "imgur.com", "flickr.com",
    "deviantart.com", "bsky.app", "threads.net", "vsco.co",
}

# سایت‌هایی که اساساً عکس/گالری‌ان؛ gallery-dl اول، بعد yt-dlp
GALLERY_FIRST_DOMAINS = {"pixiv.net", "deviantart.com", "flickr.com", "tumblr.com", "imgur.com", "vsco.co"}

DIRECT_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".mp3", ".flac", ".wav", ".ogg",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
}


class DownloadUserError(Exception):
    """خطایی که پیامش امنه و مستقیم به کاربر نشون داده می‌شه (حجم زیاد، دیسک پر، ...)."""


class NotVideoError(Exception):
    """yt-dlp ویدیویی تو این لینک پیدا نکرد (مثلاً پست فقط عکس داره) → می‌شه gallery-dl رو امتحان کرد."""


def _safe_filename(name: str) -> str:
    return re.sub(r'[^\w.\-]', '_', name)


def _get_domain(url: str) -> str:
    """
    دامنه رو با urllib.parse استخراج می‌کنه تا لینک‌های دارای userinfo
    (user:pass@host)، پورت (host:8080) یا IPv6 درست پارس بشن.
    """
    u = url.strip().lower()
    if "://" not in u:
        u = "http://" + u  # تا urlparse بتونه netloc رو تشخیص بده
    host = urlparse(u).hostname or ""
    if host.startswith("www."):
        host = host[4:]
    return host


def _domain_in(domain: str, domains: set) -> bool:
    return any(domain == d or domain.endswith("." + d) for d in domains)


def is_direct_link(url: str) -> bool:
    domain = _get_domain(url)
    if _domain_in(domain, YTDLP_DOMAINS):
        return False
    # پسوند رو از path می‌گیریم (نه از کل URL) تا query و #fragment تاثیری نذارن
    u = url.strip()
    if "://" not in u:
        u = "http://" + u
    path = urlparse(u).path.lower()
    return any(path.endswith(ext) for ext in DIRECT_EXTENSIONS)


def is_gallery_first(url: str) -> bool:
    return _domain_in(_get_domain(url), GALLERY_FIRST_DOMAINS)


def is_gallery_domain(url: str) -> bool:
    return _domain_in(_get_domain(url), GALLERY_DOMAINS)


def gallery_available() -> bool:
    return importlib.util.find_spec("gallery_dl") is not None


def free_space_mb(path: str = None) -> float:
    return shutil.disk_usage(path or DOWNLOAD_DIR).free / (1024 * 1024)


def ensure_free_space(extra_mb: float = 0):
    """اگه فضای خالی کمتر از (MIN_FREE_MB + extra_mb) باشه Exception می‌ده."""
    need = MIN_FREE_MB + extra_mb
    if free_space_mb() < need:
        raise DownloadUserError(f"فضای دیسک سرور کافی نیست (حداقل {need:.0f}MB خالی لازمه).")


# ─── آرگومان‌های مشترک yt-dlp / gallery-dl ─────────────────────

def _cookies_args() -> list:
    if COOKIES_FILE and os.path.isfile(COOKIES_FILE):
        return ["--cookies", COOKIES_FILE]
    return []


def _proxy_args() -> list:
    return ["--proxy", PROXY] if PROXY else []


def _js_runtime_args() -> list:
    """
    یوتیوب برای حل چالش JS به یک runtime بیرونی نیاز داره (+ بسته‌ی yt-dlp-ejs
    که با yt-dlp[default] نصب می‌شه). deno به‌صورت پیش‌فرض فعاله؛ اگه فقط node
    داریم صراحتاً فعالش می‌کنیم. با JS_RUNTIME در .env هم می‌شه override کرد.
    """
    if JS_RUNTIME:
        return ["--js-runtimes", JS_RUNTIME]
    if shutil.which("deno"):
        return []
    if shutil.which("node"):
        return ["--js-runtimes", "node"]
    return []


def _ytdlp_common_args() -> list:
    return (
        YTDLP_CMD
        + ["--no-warnings", "--socket-timeout", "30", "--retries", "5", "--fragment-retries", "5"]
        + _proxy_args() + _cookies_args() + _js_runtime_args()
    )


def _kill(proc):
    try:
        if proc and proc.returncode is None:
            proc.kill()
    except ProcessLookupError:
        pass
    except Exception:
        logger.debug("kill failed", exc_info=True)


def _remove_quietly(*paths):
    for p in paths:
        try:
            if p and os.path.isfile(p):
                os.remove(p)
        except OSError:
            pass


# ─── دانلود لینک مستقیم (wget) ─────────────────────────────────

async def _kill_if_oversized(proc, save_path: str, max_bytes: int, stop_event: asyncio.Event,
                             cancel_check=None, progress_cb=None):
    """
    گزینه‌های محدودکننده‌ی خود wget (مثل --quota) برای دانلود یک فایل تکی اثری
    ندارن. این تابع به‌صورت موازی هر ۲۰۰ میلی‌ثانیه حجم فایل در حال نوشتن رو
    چک می‌کنه و اگه از سقف رد بشه (یا کاربر لغو کنه)، پروسه‌ی wget رو می‌کشه.
    اگه progress_cb داده بشه، هر چند ثانیه حجم فعلی رو گزارش می‌ده.
    """
    last_report = 0.0
    try:
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
            size = os.path.getsize(save_path) if os.path.exists(save_path) else 0
            if size > max_bytes or (cancel_check and cancel_check()):
                _kill(proc)
                return
            now = time.monotonic()
            if progress_cb and size and now - last_report >= PROGRESS_INTERVAL:
                last_report = now
                try:
                    await progress_cb(f"📥 {size / (1024 * 1024):.1f} MB دانلود شد")
                except Exception:
                    pass
    except asyncio.CancelledError:
        pass


async def download_direct_link(uid: str, url: str, progress_cb=None, cancel_check=None) -> str:
    raw_name = urlparse(url).path.split("/")[-1] or f"file_{uid}"
    filename = _safe_filename(raw_name)[:100]
    save_path = os.path.join(DOWNLOAD_DIR, f"link_{uid}_{filename}")
    max_bytes = MAX_SIZE_MB * 1024 * 1024

    ensure_free_space()

    # ترتیب چهار آرگومان اول ثابته: wget -q -O <save_path>
    proc = await asyncio.create_subprocess_exec(
        "wget", "-q", "-O", save_path,
        "--timeout=30", "--tries=3", "--",
        url,
        stderr=asyncio.subprocess.PIPE,
    )

    stop_event = asyncio.Event()
    watcher = asyncio.get_running_loop().create_task(
        _kill_if_oversized(proc, save_path, max_bytes, stop_event, cancel_check, progress_cb)
    )
    try:
        _, stderr = await proc.communicate()
    except BaseException:
        # لغو شدن تسک یا هر خطای دیگه: پروسه‌ی wget رو یتیم نذار
        _kill(proc)
        _remove_quietly(save_path)
        raise
    finally:
        stop_event.set()
        watcher.cancel()

    cancelled = bool(cancel_check and cancel_check())
    oversized = os.path.exists(save_path) and os.path.getsize(save_path) > max_bytes

    if cancelled or proc.returncode != 0 or oversized:
        _remove_quietly(save_path)
        if cancelled:
            raise asyncio.CancelledError()
        if oversized:
            raise DownloadUserError(f"حجم فایل بیشتر از حد مجاز ({MAX_SIZE_MB}MB) بود؛ دانلود متوقف شد.")
        raise Exception(stderr.decode(errors="replace") if stderr else "خطای نامشخص wget")

    return save_path


# ─── اطلاعات ویدیو (yt-dlp) ────────────────────────────────────

_NOT_VIDEO_HINTS = (
    "there is no video in this post",
    "no video could be found",
    "no video formats found",
    "no media found",
    "unsupported url",
    "unable to extract",
)


async def get_video_info(url: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *_ytdlp_common_args(), "--dump-json", "--no-playlist", "--", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=120)
    except asyncio.TimeoutError:
        _kill(proc)
        raise DownloadUserError("زمان بررسی لینک تموم شد (۲ دقیقه).")
    except BaseException:
        _kill(proc)
        raise

    if proc.returncode != 0:
        err = stderr.decode(errors="replace")
        low = err.lower()
        if "sign in to confirm" in low or "not a bot" in low:
            raise DownloadUserError("یوتیوب درخواست رو به‌عنوان ربات تشخیص داد؛ فایل کوکی (COOKIES_FILE) رو تنظیم کن.")
        if "private" in low or "login required" in low or "log in" in low:
            raise DownloadUserError("این محتوا خصوصیه یا نیاز به لاگین (کوکی) داره.")
        if any(h in low for h in _NOT_VIDEO_HINTS):
            raise NotVideoError(err.strip()[-300:])
        raise Exception(err.strip()[-300:])

    info = json.loads(stdout.decode(errors="replace"))
    return {
        "title":    info.get("title") or "video",
        "duration": info.get("duration_string") or "نامشخص",
        "ext":      info.get("ext", "mp4"),
        "formats":  _parse_formats(info),
    }


def _mb(size) -> float:
    return round(size / (1024 * 1024), 1) if size else 0


def _parse_formats(info: dict) -> list:
    """
    گزینه‌های کیفیت رو می‌سازه. به‌جای استفاده‌ی مستقیم از format_id (که برای
    یوتیوب معمولاً «فقط ویدیو، بدون صدا» می‌شد)، برای هر ارتفاع یک selector
    می‌سازیم که بهترین ویدیو تا اون ارتفاع + بهترین صدا رو ادغام می‌کنه.
    هر آیتم: format_id (کلید کوتاه برای callback_data)، selector (برای -f)، label، ...
    """
    formats = info.get("formats") or []

    heights = {}          # height -> بزرگ‌ترین حجم شناخته‌شده
    best_audio_size = 0
    has_video = False
    for f in formats:
        # None یعنی «نامشخص» (مثلاً extractor عمومی) و فقط "none" صریح یعنی «ندارد»
        vcodec = f.get("vcodec")
        acodec = f.get("acodec")
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if vcodec != "none":
            has_video = True
            h = f.get("height")
            if h:
                heights[h] = max(heights.get(h, 0), size)
        elif acodec not in ("none", None):
            best_audio_size = max(best_audio_size, size)

    result = []
    for h in sorted(heights, reverse=True)[:5]:
        est = heights[h] + best_audio_size if heights[h] else 0
        result.append({
            "format_id":   f"v{h}",
            "selector":    f"bv*[height<={h}]+ba/b[height<={h}]",
            "label":       f"🎬 {h}p",
            "height":      h,
            "filesize_mb": _mb(est),
            "has_audio":   True,
            "type":        "video",
        })

    if has_video and not result:
        result.append({
            "format_id": "vbest", "selector": "bv*+ba/b", "label": "🎬 بهترین کیفیت",
            "height": 9999, "filesize_mb": 0, "has_audio": True, "type": "video",
        })

    # گزینه‌ی صوتی همیشه هست (mp3)
    result.append({
        "format_id": "abest", "selector": "ba/b", "label": "🎵 MP3 (بهترین کیفیت)",
        "height": 0, "filesize_mb": _mb(best_audio_size), "has_audio": True, "type": "audio",
    })
    return result


# ─── دانلود ویدیو/صوت با yt-dlp ────────────────────────────────

def _tag_files(tag: str) -> list:
    return glob.glob(os.path.join(glob.escape(DOWNLOAD_DIR), f"ytdlp_{tag}_*"))


async def download_video_from_page(
    uid: str,
    url: str,
    selector: str = "bv*+ba/b",
    progress_cb=None,
    audio_only: bool = False,
    cancel_check=None,
) -> str:
    ensure_free_space()
    tag = f"{uid}_{secrets.token_hex(3)}"
    output_template = os.path.join(DOWNLOAD_DIR, f"ytdlp_{tag}_%(title).40s.%(ext)s")

    cmd = _ytdlp_common_args() + [
        "--no-playlist", "--newline", "--progress", "--no-simulate",
        "--progress-template",
        "download:PGR|%(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
        "--print", "after_move:filepath",
        "--max-filesize", f"{MAX_SIZE_MB}M",
        "-o", output_template,
    ]
    if audio_only:
        cmd += ["-f", selector, "-x", "--audio-format", "mp3", "--audio-quality", "0"]
    else:
        # بین گزینه‌های هم‌کیفیت، H.264/AAC رو ترجیح بده (سازگاری بیشتر با پخش‌کننده‌ها)
        cmd += ["-f", selector, "-S", "res,vcodec:h264,acodec:m4a", "--merge-output-format", "mp4"]
    cmd += ["--", url]

    # stderr رو با stdout ادغام می‌کنیم؛ وگرنه اگه yt-dlp زیاد روی stderr بنویسه
    # و کسی نخونتش، بافر پایپ پر می‌شه و پروسه قفل می‌کنه (deadlock).
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )

    final_path = None
    tail = []
    last_report = 0.0
    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            if cancel_check and cancel_check():
                _kill(proc)
                break
            if line.startswith("PGR|"):
                now = time.monotonic()
                if progress_cb and now - last_report >= PROGRESS_INTERVAL:
                    last_report = now
                    _, pct, speed, eta = (line.split("|") + ["", "", ""])[:4]
                    try:
                        await progress_cb(f"📥 {pct.strip()} — {speed.strip()} — ⏱ {eta.strip()}")
                    except Exception:
                        pass
                continue
            if os.path.isabs(line) and os.path.isfile(line):
                final_path = line     # خروجی --print after_move:filepath
            else:
                tail.append(line)
                tail = tail[-8:]
        await proc.wait()
    except BaseException:
        _kill(proc)
        for p in _tag_files(tag):
            _remove_quietly(p)
        raise

    if cancel_check and cancel_check():
        for p in _tag_files(tag):
            _remove_quietly(p)
        raise asyncio.CancelledError()

    if proc.returncode != 0:
        for p in _tag_files(tag):
            _remove_quietly(p)
        msg = "\n".join(tail)[-300:]
        if "larger than max-filesize" in msg.lower():
            raise DownloadUserError(f"حجم فایل بیشتر از حد مجاز ({MAX_SIZE_MB}MB) بود.")
        raise Exception(f"yt-dlp خطا داد:\n{msg}")

    if not final_path or not os.path.exists(final_path):
        # fallback: جدیدترین خروجی کامل با همین tag (بدون فایل‌های موقت)
        files = [p for p in _tag_files(tag) if not p.endswith((".part", ".ytdl", ".temp"))]
        if not files:
            raise Exception("فایل دانلود‌شده پیدا نشد!")
        final_path = max(files, key=os.path.getmtime)

    # فایل‌های جانبی احتمالی (مثلاً بخش‌های ادغام‌نشده) رو پاک کن
    for p in _tag_files(tag):
        if p != final_path:
            _remove_quietly(p)
    return final_path


# ─── دانلود عکس/گالری با gallery-dl ────────────────────────────

async def download_gallery(uid: str, url: str, progress_cb=None, cancel_check=None) -> list:
    """
    عکس/ویدیوهای یک پست (کاروسل اینستاگرام، توییت چندعکسه، پین، ...) رو دانلود
    می‌کنه و لیست مسیر فایل‌ها (داخل DOWNLOAD_DIR) رو برمی‌گردونه.
    حداکثر GALLERY_MAX_FILES فایل (تا اگه لینک پروفایل بود، کل اکانت دانلود نشه).
    """
    if not gallery_available():
        raise DownloadUserError("gallery-dl نصب نیست (pip install gallery-dl).")
    ensure_free_space()

    job = f"{uid}_{secrets.token_hex(3)}"
    out_dir = os.path.join(DOWNLOAD_DIR, f".gdl_{job}")
    os.makedirs(out_dir, exist_ok=True)

    cmd = GALLERY_CMD + [
        "-D", out_dir,
        "--range", f"1-{GALLERY_MAX_FILES}",
        "--filesize-max", f"{MAX_SIZE_MB}M",
        "--retries", "3", "--http-timeout", "30",
    ] + _proxy_args() + _cookies_args() + ["--", url]

    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )

    listed = []   # مسیرهایی که gallery-dl چاپ کرده (به ترتیب دانلود)
    tail = []
    last_report = 0.0
    try:
        async for raw in proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            if cancel_check and cancel_check():
                _kill(proc)
                break
            if line.startswith(out_dir):
                listed.append(line)
                now = time.monotonic()
                if progress_cb and now - last_report >= PROGRESS_INTERVAL:
                    last_report = now
                    try:
                        await progress_cb(f"🖼 {len(listed)} فایل دانلود شد...")
                    except Exception:
                        pass
            elif not line.startswith("#"):
                tail.append(line)
                tail = tail[-6:]
        await proc.wait()
    except BaseException:
        _kill(proc)
        shutil.rmtree(out_dir, ignore_errors=True)
        raise

    if cancel_check and cancel_check():
        shutil.rmtree(out_dir, ignore_errors=True)
        raise asyncio.CancelledError()

    # ترتیب چاپ‌شده + هر فایل دیگه‌ای که توی پوشه مونده
    on_disk = sorted(
        p for p in glob.glob(os.path.join(glob.escape(out_dir), "*"))
        if os.path.isfile(p) and not p.endswith((".part", ".tmp"))
    )
    ordered = [p for p in listed if os.path.isfile(p)]
    ordered += [p for p in on_disk if p not in ordered]

    if not ordered:
        shutil.rmtree(out_dir, ignore_errors=True)
        detail = "\n".join(tail)[-300:] or "فایلی پیدا نشد."
        raise Exception(f"gallery-dl چیزی دانلود نکرد:\n{detail}")

    # انتقال به DOWNLOAD_DIR با اسم یکتا و حذف پوشه‌ی موقت
    results = []
    for idx, p in enumerate(ordered, 1):
        ext = os.path.splitext(p)[1].lower() or ".bin"
        dest = os.path.join(DOWNLOAD_DIR, f"gdl_{job}_{idx:02d}{ext}")
        shutil.move(p, dest)
        results.append(dest)
    shutil.rmtree(out_dir, ignore_errors=True)
    return results
