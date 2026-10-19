"""
تست‌های موتورهای دانلود (yt-dlp / gallery-dl): پارس کیفیت‌ها، دسته‌بندی سایت‌ها،
و تست یکپارچه با یک سرور HTTP محلی (بدون نیاز به اینترنت).
"""
import os
import sys
import shutil
import asyncio
import functools
import threading
import subprocess
import http.server
import importlib.util

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloader
import telegram_bot as tb


# ─── _parse_formats ─────────────────────────────────────────────

def _yt_like_info():
    fmts = [
        {"format_id": "18",  "vcodec": "avc1", "acodec": "mp4a", "height": 360,  "filesize": 10_000_000},
        {"format_id": "137", "vcodec": "avc1", "acodec": "none", "height": 1080, "filesize": 80_000_000},
        {"format_id": "136", "vcodec": "avc1", "acodec": "none", "height": 720,  "filesize": 40_000_000},
        {"format_id": "247", "vcodec": "vp9",  "acodec": "none", "height": 720,  "filesize": 30_000_000},
        {"format_id": "140", "vcodec": "none", "acodec": "mp4a", "filesize": 5_000_000},
        {"format_id": "sb0", "vcodec": "none", "acodec": "none", "height": 90},      # storyboard
    ]
    return {"formats": fmts}


def test_parse_formats_builds_height_selectors_with_audio_merge():
    fm = downloader._parse_formats(_yt_like_info())
    videos = [f for f in fm if f["type"] == "video"]
    assert [f["height"] for f in videos] == [1080, 720, 360]            # بالا به پایین، بدون storyboard
    assert videos[0]["selector"] == "bv*[height<=1080]+ba/b[height<=1080]"  # صدا هم ادغام می‌شه
    assert videos[0]["filesize_mb"] == round(85_000_000 / 1048576, 1)       # ویدیو + صدا
    assert fm[-1]["type"] == "audio" and fm[-1]["format_id"] == "abest"


def test_parse_formats_unknown_codec_is_treated_as_video():
    """باگی که تست یکپارچه گرفت: vcodec=None (نامشخص) نباید یعنی «بدون ویدیو»."""
    fm = downloader._parse_formats({"formats": [{"format_id": "0", "vcodec": None, "acodec": None}]})
    assert [f["format_id"] for f in fm] == ["vbest", "abest"]


def test_parse_formats_limits_to_five_heights():
    info = {"formats": [{"format_id": str(h), "vcodec": "avc1", "acodec": "none", "height": h}
                        for h in (144, 240, 360, 480, 720, 1080, 1440, 2160)]}
    videos = [f for f in downloader._parse_formats(info) if f["type"] == "video"]
    assert len(videos) == 5 and videos[0]["height"] == 2160


def test_callback_data_fits_telegram_limit():
    uid = "5123456789"
    for f in downloader._parse_formats(_yt_like_info()):
        assert len(f"ytdlp:{uid}:{f['format_id']}".encode()) <= 64


# ─── دسته‌بندی سایت‌ها ──────────────────────────────────────────

def test_is_direct_link_ignores_fragment_and_query():
    assert downloader.is_direct_link("https://example.com/a.zip#section")
    assert downloader.is_direct_link("https://example.com/a.zip?x=1#y")
    assert not downloader.is_direct_link("https://example.com/watch#a.zip")


def test_gallery_domain_helpers():
    assert downloader.is_gallery_domain("https://www.instagram.com/p/abc/")
    assert downloader.is_gallery_domain("https://mobile.twitter.com/u/status/1")
    assert not downloader.is_gallery_domain("https://www.youtube.com/watch?v=1")
    assert downloader.is_gallery_first("https://www.pixiv.net/en/artworks/1")
    assert not downloader.is_gallery_first("https://www.instagram.com/p/abc/")


# ─── هندلر متن و کیبوردها ───────────────────────────────────────

class _Msg:
    def __init__(self, text):
        self.text = text
        self.from_user = type("U", (), {"id": 12345})()
        self.replies = []

    async def reply_text(self, t, **k):
        self.replies.append(t)


class _Upd:
    def __init__(self, text):
        self.message = _Msg(text)
        self.effective_user = self.message.from_user


async def test_handle_text_extracts_link_from_surrounding_text(monkeypatch):
    monkeypatch.setattr(tb, "ALLOWED_USER_ID", 12345)
    got = []

    async def fake(uid, url, message, context):
        got.append(url)

    monkeypatch.setattr(tb, "handle_link_download", fake)
    await tb.handle_text(_Upd("ببین این رو https://x.com/u/status/1?s=20 جالبه"), None)
    assert got == ["https://x.com/u/status/1?s=20"]


async def test_handle_text_ignores_plain_text(monkeypatch):
    monkeypatch.setattr(tb, "ALLOWED_USER_ID", 12345)
    got = []

    async def fake(*a, **k):
        got.append(1)

    monkeypatch.setattr(tb, "handle_link_download", fake)
    await tb.handle_text(_Upd("سلام"), None)
    assert got == []


def test_inline_keyboards_build_with_real_ptb_style():
    """باگ اصلی: با PTB<22.7 ساخت دکمه با style= خطا می‌داد."""
    for kb in (tb.send_keyboard("1"), tb.more_keyboard("1"), tb.cancel_keyboard("1"), tb.link_cancel_keyboard("1")):
        assert kb is not None
    btn = tb.link_cancel_keyboard("1").inline_keyboard[0][0]
    if hasattr(btn, "style"):
        assert btn.style == "danger"


# ─── تست یکپارچه با سرور محلی (yt-dlp و gallery-dl واقعی) ──────────

_have_engines = all(importlib.util.find_spec(m) for m in ("yt_dlp", "gallery_dl"))
_have_ffmpeg = shutil.which("ffmpeg") is not None


@pytest.fixture(scope="module")
def media_server(tmp_path_factory):
    root = tmp_path_factory.mktemp("srv")
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240:rate=25",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-c:a", "aac", "-shortest",
         str(root / "clip.mp4")], check=True)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc=size=160x120",
                    "-frames:v", "1", str(root / "pic.jpg")], check=True)
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


@pytest.fixture
def dl_dir(tmp_path, monkeypatch):
    d = tmp_path / "dl"
    d.mkdir()
    monkeypatch.setattr(downloader, "DOWNLOAD_DIR", str(d) + "/")
    return d


@pytest.mark.skipif(not (_have_engines and _have_ffmpeg), reason="yt-dlp/gallery-dl/ffmpeg لازمه")
class TestEnginesIntegration:
    async def test_video_download_reports_final_path_and_leaves_no_temp_files(self, media_server, dl_dir):
        info = await downloader.get_video_info(f"{media_server}/clip.mp4")
        sel = next(f["selector"] for f in info["formats"] if f["type"] == "video")
        path = await downloader.download_video_from_page("7", f"{media_server}/clip.mp4", sel)
        assert os.path.isfile(path) and os.path.getsize(path) > 1000
        assert os.listdir(dl_dir) == [os.path.basename(path)]

    async def test_audio_extraction_to_mp3(self, media_server, dl_dir):
        path = await downloader.download_video_from_page(
            "7", f"{media_server}/clip.mp4", "ba/b", audio_only=True)
        assert path.endswith(".mp3") and os.path.getsize(path) > 500

    async def test_cancel_raises_and_cleans_up(self, media_server, dl_dir):
        with pytest.raises(asyncio.CancelledError):
            await downloader.download_video_from_page(
                "7", f"{media_server}/clip.mp4", "bv*+ba/b", cancel_check=lambda: True)
        assert os.listdir(dl_dir) == []

    async def test_gallery_dl_downloads_image_and_cleans_temp_dir(self, media_server, dl_dir):
        files = await downloader.download_gallery("7", f"{media_server}/pic.jpg")
        assert len(files) == 1 and files[0].endswith(".jpg") and os.path.getsize(files[0]) > 100
        assert not [n for n in os.listdir(dl_dir) if n.startswith(".gdl")]

    async def test_gallery_failure_cleans_temp_dir(self, media_server, dl_dir):
        with pytest.raises(Exception):
            await downloader.download_gallery("7", f"{media_server}/missing.jpg")
        assert os.listdir(dl_dir) == []

    async def test_max_size_is_enforced_for_ytdlp(self, media_server, dl_dir, monkeypatch):
        # سقف خیلی کوچیک؛ yt-dlp باید با --max-filesize رد کنه (نه این‌که دیسک رو پر کنه)
        monkeypatch.setattr(downloader, "MAX_SIZE_MB", 0)
        with pytest.raises(Exception):
            await downloader.download_video_from_page("7", f"{media_server}/clip.mp4", "bv*+ba/b")
        assert os.listdir(dl_dir) == []
