"""
تست‌های session_store.py — ذخیره/بازیابی متادیتای session روی دیسک.
"""
import os
import sys
import importlib

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _fresh_session_store(base_dir):
    """
    یک نسخه‌ی تازه از ماژول session_store رو با BASE_DIR اشاره‌شده به
    base_dir بارگذاری می‌کنه، تا هر تست فایل JSON مستقل خودش رو داشته باشه
    (بدون تداخل با تست‌های دیگه یا فایل واقعی پروژه).
    """
    import config
    config.BASE_DIR = str(base_dir)

    if "session_store" in sys.modules:
        del sys.modules["session_store"]
    import session_store
    importlib.reload(session_store)
    session_store.SESSIONS_FILE = os.path.join(str(base_dir), "active_sessions.json")
    return session_store


def test_save_and_load_roundtrip(tmp_path):
    store = _fresh_session_store(tmp_path)
    sessions = {
        "111": {"files": [], "waiting": False, "start_from": 0, "retries": {}},
    }
    store.save_all(sessions)
    loaded = store.load_all()
    assert loaded == sessions


def test_non_persistable_keys_are_dropped(tmp_path):
    """
    کلیدهایی مثل pending_forwards (شیء Message تلگرام) و collecting_forwards
    نباید در فایل ذخیره‌شده حضور داشته باشن، چون قابل serialize نیستن یا
    بعد از ری‌استارت دیگه معنی ندارن.
    """
    store = _fresh_session_store(tmp_path)
    sessions = {
        "222": {
            "files": [],
            "waiting": False,
            "pending_forwards": ["not_serializable_message_object"],
            "collecting_forwards": True,
        }
    }
    store.save_all(sessions)
    loaded = store.load_all()
    assert "pending_forwards" not in loaded["222"]
    assert "collecting_forwards" not in loaded["222"]
    assert "waiting" in loaded["222"]


def test_load_all_missing_file_returns_empty_dict(tmp_path):
    store = _fresh_session_store(tmp_path)
    assert store.load_all() == {}


def test_load_all_corrupted_file_returns_empty_dict(tmp_path):
    store = _fresh_session_store(tmp_path)
    with open(store.SESSIONS_FILE, "w", encoding="utf-8") as f:
        f.write("{not valid json")
    assert store.load_all() == {}


def test_save_all_is_atomic_no_leftover_tmp_file(tmp_path):
    store = _fresh_session_store(tmp_path)
    store.save_all({"1": {"files": [], "waiting": False}})
    assert not os.path.exists(store.SESSIONS_FILE + ".tmp")
    assert os.path.exists(store.SESSIONS_FILE)


def test_recover_sessions_keeps_only_existing_files(tmp_path):
    store = _fresh_session_store(tmp_path)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    f1 = str(downloads / "a.mp4")
    f2 = str(downloads / "b.mp4")
    with open(f1, "wb") as f:
        f.write(b"data-a")
    with open(f2, "wb") as f:
        f.write(b"data-b")

    store.save_all({
        "333": {
            "files": [
                {"path": f1, "name": "a.mp4"},
                {"path": f2, "name": "b.mp4"},
            ],
            "waiting": False,
        }
    })

    # فایل دوم رو "پاک" می‌کنیم (شبیه‌سازی ارسال موفق قبل از کرش)
    os.remove(f2)

    recovered = store.recover_sessions()
    assert "333" in recovered
    assert len(recovered["333"]["files"]) == 1
    assert recovered["333"]["files"][0]["name"] == "a.mp4"


def test_recover_sessions_drops_session_with_no_remaining_files(tmp_path):
    store = _fresh_session_store(tmp_path)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    f1 = str(downloads / "gone.mp4")
    with open(f1, "wb") as f:
        f.write(b"data")

    store.save_all({"444": {"files": [{"path": f1, "name": "gone.mp4"}], "waiting": False}})
    os.remove(f1)  # هیچ فایلی از این session باقی نمی‌مونه

    recovered = store.recover_sessions()
    assert recovered == {}
    # فایل JSON هم باید پاک شده باشه چون چیزی برای بازیابی نمونده
    assert not os.path.exists(store.SESSIONS_FILE)


def test_recover_sessions_with_no_saved_file(tmp_path):
    store = _fresh_session_store(tmp_path)
    assert store.recover_sessions() == {}


def test_clear_removes_file(tmp_path):
    store = _fresh_session_store(tmp_path)
    store.save_all({"1": {"files": [], "waiting": False}})
    assert os.path.exists(store.SESSIONS_FILE)
    store.clear()
    assert not os.path.exists(store.SESSIONS_FILE)


def test_save_all_never_raises_on_non_serializable_value(tmp_path):
    """
    اگه به هر دلیلی (مثلاً یک باگ آینده) یک مقدار غیرقابل‌serialize داخل
    یکی از کلیدهای persistable قرار بگیره، save_all نباید کرش کنه؛ چون
    این یک لایه‌ی best-effort است و نباید جریان اصلی ربات رو مختل کنه.
    """
    store = _fresh_session_store(tmp_path)

    class NotSerializable:
        pass

    # "files" یکی از کلیدهای persistable هست؛ مقداری غیرقابل‌json می‌ذاریم
    sessions = {"1": {"files": NotSerializable(), "waiting": False}}
    store.save_all(sessions)  # نباید هیچ Exception ای بالا بیاد
    assert True  # اگه به این خط رسیدیم یعنی کرش نکرده


def test_recover_sessions_with_part_files(tmp_path):
    """
    session‌های حالت زیپ/split هم part_files دارن؛ recover_sessions باید
    این‌ها رو هم مثل files فیلتر کنه (فقط پارت‌های موجود روی دیسک نگه
    داشته بشن).
    """
    store = _fresh_session_store(tmp_path)
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    part1 = str(downloads / "archive.7z.001")
    part2 = str(downloads / "archive.7z.002")
    with open(part1, "wb") as f:
        f.write(b"part1")
    with open(part2, "wb") as f:
        f.write(b"part2")

    store.save_all({
        "555": {
            "files": [{"path": str(downloads / "orig.mp4"), "name": "orig.mp4"}],
            "part_files": [part1, part2],
            "waiting": False,
            "safe_mode": True,
        }
    })
    # فایل اصلی که آرشیو شده دیگه روی دیسک نیست (چون بعد از ساخت آرشیو پاک می‌شه)
    # ولی پارت‌ها هنوز هستن.
    recovered = store.recover_sessions()
    assert "555" in recovered
    assert recovered["555"]["part_files"] == [part1, part2]
    assert recovered["555"]["files"] == []  # orig.mp4 دیگه وجود نداره
