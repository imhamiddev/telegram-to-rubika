"""
تست‌های stats.py — ثبت آمار دانلود/ارسال/آرشیو و WAL mode.
"""
import os
import sys
import sqlite3
import importlib
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_stats(base_dir):
    """
    یک نسخه‌ی تازه از ماژول stats با فایل دیتابیس مستقل در base_dir بارگذاری
    می‌کنه تا تست‌ها با هم و با دیتابیس واقعی پروژه تداخل نکنن.
    """
    if "stats" in sys.modules:
        del sys.modules["stats"]
    import stats
    stats._con.close()
    db_path = os.path.join(str(base_dir), "test_stats.db")
    stats.DB_PATH = db_path
    stats._con = sqlite3.connect(db_path, check_same_thread=False)
    stats._con.execute("PRAGMA journal_mode=WAL")
    stats._con.execute("PRAGMA synchronous=NORMAL")
    stats._db_initialized = False
    return stats


def test_lazy_init_creates_table_on_first_use(tmp_path):
    stats = _fresh_stats(tmp_path)
    assert stats._db_initialized is False
    stats.log_download(10.0)
    assert stats._db_initialized is True


def test_log_and_get_stats_download(tmp_path):
    stats = _fresh_stats(tmp_path)
    stats.log_download(100.0)
    stats.log_download(50.0)
    result = stats.get_stats(1)
    assert result["download"]["count"] == 2
    assert abs(result["download"]["gb"] - 150.0 / 1024) < 1e-9


def test_log_send_and_archive(tmp_path):
    stats = _fresh_stats(tmp_path)
    stats.log_send(20.0)
    stats.log_archive(40.0, parts=3)
    result = stats.get_stats(1)
    assert result["send"]["count"] == 1
    assert result["archive"]["count"] == 1
    assert result["archive"]["parts"] == 3


def test_get_stats_respects_time_window(tmp_path):
    """رویدادهای خیلی قدیمی‌تر از بازه‌ی درخواستی نباید در نتیجه لحاظ بشن."""
    import time
    stats = _fresh_stats(tmp_path)
    stats.init_db()  # قبل از insert دستی به _con، مطمئن می‌شیم جدول ساخته شده
    old_ts = time.time() - 40 * 86400  # ۴۰ روز پیش
    with stats._con:
        stats._con.execute(
            "INSERT INTO events (ts, event, size_mb) VALUES (?, 'download', ?)",
            (old_ts, 999.0),
        )
    stats.log_download(5.0)  # این یکی الانه

    result_30d = stats.get_stats(30)
    assert result_30d["download"]["count"] == 1  # فقط رکورد جدید
    result_all = stats.get_stats(9999)
    assert result_all["download"]["count"] == 2  # هر دو


def test_index_exists_on_events_table(tmp_path):
    stats = _fresh_stats(tmp_path)
    stats.log_download(1.0)  # فورس init_db از طریق lazy init
    idx_names = [
        row[0] for row in
        stats._con.execute("SELECT name FROM sqlite_master WHERE type='index'").fetchall()
    ]
    assert "idx_events_event_ts" in idx_names


def test_wal_mode_enabled(tmp_path):
    stats = _fresh_stats(tmp_path)
    mode = stats._con.execute("PRAGMA journal_mode").fetchone()[0]
    assert mode.lower() == "wal"


def test_format_stats_text_contains_expected_sections(tmp_path):
    stats = _fresh_stats(tmp_path)
    stats.log_download(10.0)
    text = stats.format_stats_text()
    assert "آمار ربات" in text
    assert "امروز" in text
    assert "۷ روز گذشته" in text
    assert "۳۰ روز گذشته" in text


def test_concurrent_writes_do_not_raise_database_locked(tmp_path):
    """
    قبل از معرفی WAL mode، نوشتن هم‌زمان از چند thread می‌تونست به خطای
    'database is locked' منجر بشه. این تست همون سناریو رو با تعداد زیادی
    نوشتن هم‌زمان شبیه‌سازی می‌کنه.
    """
    stats = _fresh_stats(tmp_path)
    errors = []

    def writer():
        try:
            for _ in range(30):
                stats.log_download(1.0)
        except Exception as e:
            errors.append(e)

    threads = [threading.Thread(target=writer) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    result = stats.get_stats(1)
    assert result["download"]["count"] == 8 * 30


def test_init_db_is_idempotent(tmp_path):
    """صدا زدن مکرر init_db نباید خطا بده (CREATE TABLE/INDEX IF NOT EXISTS)."""
    stats = _fresh_stats(tmp_path)
    stats.init_db()
    stats.init_db()
    stats.init_db()
    stats.log_download(1.0)
    assert stats.get_stats(1)["download"]["count"] == 1
