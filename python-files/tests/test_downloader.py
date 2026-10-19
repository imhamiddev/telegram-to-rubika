"""
تست‌های downloader.py — تشخیص دامنه، تشخیص لینک مستقیم، و محدودیت حجم دانلود.
"""
import os
import sys
import asyncio

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import downloader


# ─── _get_domain ────────────────────────────────────────────────

def test_get_domain_simple_https():
    assert downloader._get_domain("https://www.youtube.com/watch?v=abc") == "youtube.com"


def test_get_domain_strips_www():
    assert downloader._get_domain("https://www.example.com/x") == "example.com"


def test_get_domain_with_port():
    """
    قبل از استفاده از urllib.parse، regex قدیمی پورت رو هم بخشی از دامنه
    حساب می‌کرد (مثلاً 'x.com:443' به‌جای 'x.com').
    """
    assert downloader._get_domain("https://x.com:443/status/123") == "x.com"


def test_get_domain_with_userinfo():
    """
    لینک‌های حاوی user:pass@host باید فقط host رو برگردونن، نه کل authority.
    """
    assert downloader._get_domain("http://user:pass@instagram.com/reel/xyz") == "instagram.com"


def test_get_domain_subdomain_not_stripped_incorrectly():
    assert downloader._get_domain("http://sub.tiktok.com/video/1") == "sub.tiktok.com"


def test_get_domain_no_scheme():
    assert downloader._get_domain("example.com/file.zip") == "example.com"


def test_get_domain_empty_string():
    assert downloader._get_domain("") == ""


# ─── is_direct_link ─────────────────────────────────────────────

def test_is_direct_link_known_ytdlp_domain_is_false():
    assert downloader.is_direct_link("https://www.youtube.com/watch?v=abc") is False
    assert downloader.is_direct_link("https://twitter.com/user/status/1") is False


def test_is_direct_link_subdomain_of_ytdlp_domain_is_false():
    assert downloader.is_direct_link("https://vm.tiktok.com/xyz") is False


def test_is_direct_link_direct_file_extension_is_true():
    assert downloader.is_direct_link("https://example.com/movie.mp4") is True
    assert downloader.is_direct_link("https://cdn.example.com/archive.zip?token=123") is True


def test_is_direct_link_unknown_domain_no_extension_is_false():
    assert downloader.is_direct_link("https://example.com/some/page") is False


# ─── _safe_filename ─────────────────────────────────────────────

def test_safe_filename_replaces_unsafe_chars():
    assert downloader._safe_filename("my file!@#.mp4") == "my_file___.mp4"


def test_safe_filename_keeps_safe_chars():
    assert downloader._safe_filename("file-name_v2.final.mp4") == "file-name_v2.final.mp4"


# ─── _kill_if_oversized ──────────────────────────────────────────

async def test_kill_if_oversized_kills_process_exceeding_limit(tmp_path):
    save_path = str(tmp_path / "output.bin")
    max_bytes = 2 * 1024 * 1024  # 2MB

    # پروسه‌ای که کندتر و کنترل‌شده فایل رو می‌نویسه: هر بار ۱MB، تا ۵۰MB
    writer_script = str(tmp_path / "writer.py")
    with open(writer_script, "w") as f:
        f.write(
            "import sys, time\n"
            "path = sys.argv[1]\n"
            "with open(path, 'wb') as fh:\n"
            "    for _ in range(50):\n"
            "        fh.write(b'A' * (1024*1024))\n"
            "        fh.flush()\n"
            "        time.sleep(0.05)\n"
        )

    proc = await asyncio.create_subprocess_exec("python3", writer_script, save_path)
    stop_event = asyncio.Event()
    watcher = asyncio.get_event_loop().create_task(
        downloader._kill_if_oversized(proc, save_path, max_bytes, stop_event)
    )
    await proc.wait()
    stop_event.set()
    watcher.cancel()

    final_size = os.path.getsize(save_path)
    # باید خیلی زودتر از رسیدن به ۵۰MB متوقف شده باشه
    assert final_size < 10 * 1024 * 1024
    assert proc.returncode != 0  # با kill، returncode صفر نیست


async def test_kill_if_oversized_does_not_kill_small_download(tmp_path):
    save_path = str(tmp_path / "small.bin")
    max_bytes = 5 * 1024 * 1024  # 5MB

    writer_script = str(tmp_path / "small_writer.py")
    with open(writer_script, "w") as f:
        f.write(
            "import sys, time\n"
            "path = sys.argv[1]\n"
            "with open(path, 'wb') as fh:\n"
            "    for _ in range(2):\n"  # فقط ۲MB - زیر سقف
            "        fh.write(b'A' * (1024*1024))\n"
            "        fh.flush()\n"
            "        time.sleep(0.05)\n"
        )

    proc = await asyncio.create_subprocess_exec("python3", writer_script, save_path)
    stop_event = asyncio.Event()
    watcher = asyncio.get_event_loop().create_task(
        downloader._kill_if_oversized(proc, save_path, max_bytes, stop_event)
    )
    await proc.wait()
    stop_event.set()
    watcher.cancel()

    assert proc.returncode == 0  # طبیعی تموم شده، نه kill شده
    assert 1.9 * 1024 * 1024 < os.path.getsize(save_path) < 2.1 * 1024 * 1024


# ─── download_direct_link (با جایگزینی wget با یک اسکریپت کنترل‌شده) ──

async def test_download_direct_link_raises_clear_error_when_oversized(tmp_path, monkeypatch):
    import config
    config.MAX_SIZE_MB = 5
    monkeypatch.setattr(downloader, "MAX_SIZE_MB", 5)
    monkeypatch.setattr(downloader, "DOWNLOAD_DIR", str(tmp_path) + "/")

    big_writer = str(tmp_path / "big_writer.py")
    with open(big_writer, "w") as f:
        f.write(
            "import sys, time\n"
            "path = sys.argv[1]\n"
            "with open(path, 'wb') as fh:\n"
            "    for _ in range(200):\n"
            "        fh.write(b'A' * (1024*1024))\n"
            "        fh.flush()\n"
            "        time.sleep(0.05)\n"
        )

    orig_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        save_path = args[3]  # ("wget", "-q", "-O", save_path, url)
        return await orig_exec("python3", big_writer, save_path, stderr=kwargs.get("stderr"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    raised = False
    try:
        await downloader.download_direct_link("uid1", "http://fake/file.bin")
    except Exception as e:
        raised = True
        assert "حجم فایل" in str(e)
    assert raised, "باید به‌خاطر عبور از سقف حجم Exception پرتاب می‌شد"

    # فایل ناقص باید پاک شده باشه
    leftover = [f for f in os.listdir(tmp_path) if f.startswith("link_uid1_")]
    assert leftover == []


async def test_download_direct_link_succeeds_for_small_file(tmp_path, monkeypatch):
    monkeypatch.setattr(downloader, "MAX_SIZE_MB", 5)
    monkeypatch.setattr(downloader, "DOWNLOAD_DIR", str(tmp_path) + "/")

    small_writer = str(tmp_path / "small_writer2.py")
    with open(small_writer, "w") as f:
        f.write(
            "import sys\n"
            "path = sys.argv[1]\n"
            "with open(path, 'wb') as fh:\n"
            "    fh.write(b'A' * (1024*1024))\n"  # 1MB - زیر سقف
        )

    orig_exec = asyncio.create_subprocess_exec

    async def fake_exec(*args, **kwargs):
        save_path = args[3]
        return await orig_exec("python3", small_writer, save_path, stderr=kwargs.get("stderr"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    result_path = await downloader.download_direct_link("uid2", "http://fake/small.bin")
    assert os.path.exists(result_path)
    assert 0.9 * 1024 * 1024 < os.path.getsize(result_path) < 1.1 * 1024 * 1024
