"""
تست‌های telegram_bot.py — فقط توابع خالص و بدون نیاز به شبکه‌ی واقعی تلگرام
(handlerهای async که مستقیم به API تلگرام وصل می‌شن خارج از این تست‌هان،
چون تست‌شون بدون mock سنگین از کل Update/Context ارزش کمی داره و شکننده
می‌شه؛ در عوض منطق خالصی که این handlerها روش تکیه می‌کنن این‌جا تست شده).
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_bot


# ─── is_allowed ─────────────────────────────────────────────────

def test_is_allowed_correct_user(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ALLOWED_USER_ID", 12345)
    assert telegram_bot.is_allowed(12345) is True


def test_is_allowed_wrong_user(monkeypatch):
    monkeypatch.setattr(telegram_bot, "ALLOWED_USER_ID", 12345)
    assert telegram_bot.is_allowed(99999) is False


def test_is_allowed_fails_closed_when_unconfigured(monkeypatch):
    """
    اگه ALLOWED_USER_ID تنظیم نشده باشه (۰)، دسترسی باید برای همه رد بشه
    (fail-closed)، نه این‌که به‌اشتباه برای همه باز بمونه.
    """
    monkeypatch.setattr(telegram_bot, "ALLOWED_USER_ID", 0)
    assert telegram_bot.is_allowed(0) is False
    assert telegram_bot.is_allowed(12345) is False


# ─── smart_delay ────────────────────────────────────────────────

def test_smart_delay_thresholds():
    assert telegram_bot.smart_delay(5) == 10
    assert telegram_bot.smart_delay(9.9) == 10
    assert telegram_bot.smart_delay(10) == 30
    assert telegram_bot.smart_delay(49.9) == 30
    assert telegram_bot.smart_delay(50) == 60
    assert telegram_bot.smart_delay(99.9) == 60
    assert telegram_bot.smart_delay(100) == 120
    assert telegram_bot.smart_delay(5000) == 120


# ─── short_name ─────────────────────────────────────────────────

def test_short_name_truncates_long_names():
    assert telegram_bot.short_name("a" * 20, n=15) == "a" * 15 + "..."


def test_short_name_keeps_short_names_unchanged():
    assert telegram_bot.short_name("short.mp4", n=15) == "short.mp4"


def test_short_name_exact_boundary_not_truncated():
    name = "a" * 15
    assert telegram_bot.short_name(name, n=15) == name


# ─── bar ────────────────────────────────────────────────────────

def test_bar_zero_percent():
    assert telegram_bot.bar(0, width=10) == "░" * 10


def test_bar_full_percent():
    assert telegram_bot.bar(100, width=10) == "█" * 10


def test_bar_half_percent():
    assert telegram_bot.bar(50, width=10) == "█" * 5 + "░" * 5


# ─── get_shamsi ─────────────────────────────────────────────────

def test_get_shamsi_format():
    result = telegram_bot.get_shamsi()
    # فرمت مورد انتظار: YYYY/MM/DD - HH:MM
    parts = result.split(" - ")
    assert len(parts) == 2
    date_part, time_part = parts
    assert len(date_part.split("/")) == 3
    assert len(time_part.split(":")) == 2


# ─── safe_error_text ────────────────────────────────────────────

def test_safe_error_text_includes_prefix_and_generic_message():
    result = telegram_bot.safe_error_text("❌ خطا در دانلود.", Exception("internal path /etc/secret"))
    assert "❌ خطا در دانلود." in result
    assert telegram_bot.GENERIC_ERROR_MSG in result


def test_safe_error_text_does_not_leak_exception_details_to_user():
    """
    جزئیات داخلی exception (که ممکنه مسیر فایل سرور یا اطلاعات حساس داشته
    باشه) نباید در متن نمایش داده‌شده به کاربر ظاهر بشه؛ فقط لاگ می‌شه.
    """
    secret_detail = "/home/secret_user/very_specific_internal_path"
    result = telegram_bot.safe_error_text("❌ خطا.", Exception(secret_detail))
    assert secret_detail not in result


# ─── files_summary ──────────────────────────────────────────────

def test_files_summary_computes_total_size(tmp_path):
    f1 = tmp_path / "a.mp4"
    f2 = tmp_path / "b.mp4"
    f1.write_bytes(b"x" * (1024 * 1024))       # 1MB
    f2.write_bytes(b"x" * (2 * 1024 * 1024))   # 2MB

    files = [
        {"path": str(f1), "name": "a.mp4"},
        {"path": str(f2), "name": "b.mp4"},
    ]
    names, size_str, total_mb = telegram_bot.files_summary(files)
    assert "a.mp4" in names
    assert "b.mp4" in names
    assert abs(total_mb - 3.0) < 0.01
    assert size_str == "3.0MB"


def test_files_summary_skips_missing_files(tmp_path):
    f1 = tmp_path / "exists.mp4"
    f1.write_bytes(b"x" * (1024 * 1024))
    files = [
        {"path": str(f1), "name": "exists.mp4"},
        {"path": str(tmp_path / "does_not_exist.mp4"), "name": "gone.mp4"},
    ]
    names, size_str, total_mb = telegram_bot.files_summary(files)
    assert "exists.mp4" in names
    assert "gone.mp4" not in names
    assert abs(total_mb - 1.0) < 0.01


def test_files_summary_empty_list():
    names, size_str, total_mb = telegram_bot.files_summary([])
    assert names == ""
    assert total_mb == 0.0


# ─── find_sevenzip_bin ──────────────────────────────────────────

def test_find_sevenzip_bin_returns_none_when_not_found(monkeypatch):
    monkeypatch.setattr(telegram_bot, "_SEVENZIP_CANDIDATES", ["/definitely/not/a/real/path/7za"])
    assert telegram_bot.find_sevenzip_bin() is None


def test_find_sevenzip_bin_finds_absolute_path(tmp_path, monkeypatch):
    fake_bin = tmp_path / "7za"
    fake_bin.write_text("#!/bin/bash\nexit 0\n")
    os.chmod(str(fake_bin), 0o755)
    monkeypatch.setattr(telegram_bot, "_SEVENZIP_CANDIDATES", [str(fake_bin)])
    assert telegram_bot.find_sevenzip_bin() == str(fake_bin)


def test_find_sevenzip_bin_finds_via_path_lookup(tmp_path, monkeypatch):
    fake_bin = tmp_path / "7za"
    fake_bin.write_text("#!/bin/bash\nexit 0\n")
    os.chmod(str(fake_bin), 0o755)
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(telegram_bot, "_SEVENZIP_CANDIDATES", ["7za"])
    found = telegram_bot.find_sevenzip_bin()
    assert found == str(fake_bin)


# ─── create_7z error handling ───────────────────────────────────

def test_create_7z_raises_clear_error_when_binary_missing(monkeypatch):
    monkeypatch.setattr(telegram_bot, "SEVENZIP_BIN", None)
    raised = False
    try:
        telegram_bot.create_7z("/tmp/whatever", None, 0, "/tmp/nonexistent.txt")
    except Exception as e:
        raised = True
        assert "7za" in str(e) or "7z" in str(e)
        assert "پیدا نشد" in str(e)
    assert raised
