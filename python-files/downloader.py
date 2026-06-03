"""
downloader.py — ماژول دانلود فایل برای ربات تلگرام
"""

import os
import re
import json
import asyncio
import glob
from config import DOWNLOAD_DIR


# ─── سایت‌هایی که yt-dlp بهتره ازشون دانلود کنه ───────────────

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

DIRECT_EXTENSIONS = {
    ".mp4", ".mkv", ".avi", ".mov", ".webm",
    ".mp3", ".flac", ".wav", ".ogg",
    ".zip", ".rar", ".7z", ".tar", ".gz",
    ".pdf", ".docx", ".xlsx", ".pptx",
    ".jpg", ".jpeg", ".png", ".gif", ".webp",
}

def _safe_filename(name: str) -> str:
    return re.sub(r'[^\w.\-]', '_', name)

def _get_domain(url: str) -> str:
    match = re.search(r'(?:https?://)?(?:www\.)?([^/]+)', url.lower())
    return match.group(1) if match else ""

def is_direct_link(url: str) -> bool:
    domain = _get_domain(url)
    # اگه دامنه جزو سایت‌های شناخته‌شده‌ست، yt-dlp بهتره
    for d in YTDLP_DOMAINS:
        if domain == d or domain.endswith("." + d):
            return False
    # وگرنه پسوند فایل رو چک کن
    path = url.split("?")[0].lower()
    return any(path.endswith(ext) for ext in DIRECT_EXTENSIONS)


# ─── دانلود لینک مستقیم (wget) ─────────────────────────────────

async def download_direct_link(uid: str, url: str) -> str:
    raw_name = url.split("?")[0].split("/")[-1] or f"file_{uid}"
    filename = _safe_filename(raw_name)
    save_path = os.path.join(DOWNLOAD_DIR, f"link_{uid}_{filename}")

    proc = await asyncio.create_subprocess_exec(
        "wget", "-q", "-O", save_path, url,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()

    if proc.returncode != 0:
        if os.path.exists(save_path):
            os.remove(save_path)
        raise Exception(stderr.decode() if stderr else "خطای نامشخص wget")

    return save_path


# ─── اطلاعات ویدیو (yt-dlp) ────────────────────────────────────

async def get_video_info(url: str) -> dict:
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--dump-json", "--no-playlist", url,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()

    if proc.returncode != 0:
        err = stderr.decode()
        if "Unsupported URL" in err:
            raise Exception("این سایت پشتیبانی نمیشه یا محتوایی پیدا نشد.")
        if "Private" in err or "login" in err.lower():
            raise Exception("این محتوا خصوصیه یا نیاز به لاگین داره.")
        raise Exception(err[:300])

    info = json.loads(stdout.decode())
    return {
        "title":    info.get("title", "video"),
        "duration": info.get("duration_string", "نامشخص"),
        "ext":      info.get("ext", "mp4"),
        "formats":  _parse_formats(info),
    }


def _parse_formats(info: dict) -> list[dict]:
    formats = info.get("formats", [])
    result  = []

    # ── گزینه‌های ویدیویی ──────────────────────────────────────
    video_map = {}
    for f in formats:
        vcodec   = f.get("vcodec", "none")
        acodec   = f.get("acodec", "none")
        height   = f.get("height")
        filesize = f.get("filesize") or f.get("filesize_approx") or 0

        if vcodec == "none" or not height:
            continue

        has_audio = acodec != "none"
        size_mb   = round(filesize / (1024 * 1024), 1) if filesize else 0

        if height not in video_map or (has_audio and not video_map[height]["has_audio"]):
            video_map[height] = {
                "format_id":   f["format_id"],
                "label":       f"{height}p {'🎬' if has_audio else '🎬 (بدون صدا)'}",
                "height":      height,
                "filesize_mb": size_mb,
                "has_audio":   has_audio,
                "type":        "video",
            }

    # مرتب از بالاترین کیفیت
    for item in sorted(video_map.values(), key=lambda x: x["height"], reverse=True)[:4]:
        result.append(item)

    # اگه هیچ ویدیو+صدایی نداشت، گزینه merge اضافه کن
    if not any(f["has_audio"] for f in result):
        result.insert(0, {
            "format_id":   "bestvideo+bestaudio/best",
            "label":       "🎬 بهترین کیفیت (ویدیو+صدا)",
            "height":      9999,
            "filesize_mb": 0,
            "has_audio":   True,
            "type":        "video",
        })

    # ── گزینه‌های صوتی ─────────────────────────────────────────
    audio_formats = [
        f for f in formats
        if f.get("vcodec", "none") == "none"
        and f.get("acodec", "none") != "none"
        and f.get("abr")
    ]
    audio_formats.sort(key=lambda x: x.get("abr", 0), reverse=True)

    seen_abr = set()
    audio_count = 0
    for f in audio_formats:
        abr = int(f.get("abr", 0))
        if abr in seen_abr or audio_count >= 2:
            continue
        seen_abr.add(abr)
        audio_count += 1
        size_mb = round((f.get("filesize") or f.get("filesize_approx") or 0) / (1024*1024), 1)
        result.append({
            "format_id":   f["format_id"],
            "label":       f"🎵 صوتی {abr}kbps",
            "height":      0,
            "filesize_mb": size_mb,
            "has_audio":   True,
            "type":        "audio",
        })

    # اگه اصلاً صوتی جدا پیدا نشد، یه گزینه bestaudio بذار
    if audio_count == 0:
        result.append({
            "format_id":   "bestaudio/best",
            "label":       "🎵 بهترین کیفیت صوتی",
            "height":      0,
            "filesize_mb": 0,
            "has_audio":   True,
            "type":        "audio",
        })

    return result


# ─── دانلود ویدیو/صوت با yt-dlp ────────────────────────────────

async def download_video_from_page(
    uid: str,
    url: str,
    format_id: str = "bestvideo+bestaudio/best",
    progress_cb=None,
    audio_only: bool = False,
) -> str:
    output_template = os.path.join(DOWNLOAD_DIR, f"ytdlp_{uid}_%(title).40s.%(ext)s")

    cmd = ["yt-dlp", "--no-playlist", "--newline", "-o", output_template]

    if audio_only or format_id.startswith("bestaudio"):
        cmd += ["-f", format_id, "-x", "--audio-format", "mp3"]
    else:
        cmd += ["--merge-output-format", "mp4", "-f", format_id]

    cmd.append(url)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    last_file = None

    async def read_progress():
        nonlocal last_file
        async for line in proc.stdout:
            text = line.decode().strip()
            if "[download]" in text and "%" in text:
                if progress_cb:
                    try:
                        await progress_cb(text.replace("[download]", "📥").strip())
                    except Exception:
                        pass
            if "Destination:" in text:
                match = re.search(r'Destination:\s*(.+)', text)
                if match:
                    last_file = match.group(1).strip()

    await asyncio.gather(read_progress(), proc.wait())

    if proc.returncode != 0:
        err_bytes = await proc.stderr.read() if proc.stderr else b""
        raise Exception(f"yt-dlp خطا داد:\n{err_bytes.decode()[:300]}")

    if not last_file or not os.path.exists(last_file):
        pattern = os.path.join(DOWNLOAD_DIR, f"ytdlp_{uid}_*")
        files   = sorted(glob.glob(pattern), key=os.path.getmtime, reverse=True)
        if files:
            last_file = files[0]
        else:
            raise Exception("فایل دانلود‌شده پیدا نشد!")

    return last_file