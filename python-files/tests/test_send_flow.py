"""
تست‌های سناریویی جریان ارسال (do_send): خطا در میانه‌ی راه، ری‌استارت و ادامه،
آرشیو 7z رمزدار + split، لغو وسط آپلود، و باگ پسورد None.
rubpy با یک Client جعلی جایگزین می‌شه؛ بقیه‌ی منطق (صف، session_store، 7z) واقعیه.
"""
import os
import sys
import shutil
import asyncio
import subprocess

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import telegram_bot as tb
import session_store


class FakeQuery:
    def __init__(self):
        self.texts = []

    async def edit_message_text(self, text, **kw):
        self.texts.append(text)


@pytest.fixture
def env(tmp_path, monkeypatch):
    dl = tmp_path / "dl"
    dl.mkdir()
    monkeypatch.setattr(tb, "DOWNLOAD_DIR", str(dl) + "/")
    monkeypatch.setattr(session_store, "SESSIONS_FILE", str(tmp_path / "active.json"))
    monkeypatch.setattr(tb, "smart_delay", lambda mb: 0)
    monkeypatch.setattr(tb, "MAX_AUTO_RETRY", 1)
    for fn in ("log_send", "log_archive", "log_download"):
        monkeypatch.setattr(tb, fn, lambda *a, **k: None)
    tb._sessions.clear()
    tb._cancel_flags.clear()
    tb._busy.clear()
    yield dl
    tb._sessions.clear()
    tb._cancel_flags.clear()


def _make_files(dl, names):
    entries = []
    for n in names:
        p = dl / n
        p.write_bytes(n.encode() * 10)
        entries.append({"path": str(p), "name": n})
    return entries


def _restart():
    """شبیه‌سازی ری‌استارت: حافظه پاک می‌شه، فقط چیزی که روی دیسک ثبت شده برمی‌گرده."""
    tb._sessions.clear()
    tb._sessions.update(session_store.recover_sessions())


async def test_resume_after_failure_and_restart_loses_no_file(env, monkeypatch):
    """باگ قبلی: بعد از ری‌استارت، فایل وسط صف جا می‌افتاد و پاک می‌شد."""
    files = _make_files(env, ["f1.bin", "f2.bin", "f3.bin"])
    tb._sessions["1"] = {"files": list(files), "waiting": False}

    sent, state = [], {"fail_f2": True}

    async def fake_send(uid, path, caption):
        name = os.path.basename(path)
        if name == "f2.bin" and state["fail_f2"]:
            raise RuntimeError("network down")
        sent.append(name)

    monkeypatch.setattr(tb, "_send_to_rubika", fake_send)

    q = FakeQuery()
    await tb.do_send("1", False, q, files, "1MB")
    assert sent == ["f1.bin"]
    assert "ارسال با خطا" in q.texts[-1]

    _restart()
    assert [os.path.basename(f["path"]) for f in tb._sessions["1"]["files"]] == ["f2.bin", "f3.bin"]

    state["fail_f2"] = False
    await tb.do_send("1", False, FakeQuery(), tb._sessions["1"]["files"], "1MB")
    assert sent == ["f1.bin", "f2.bin", "f3.bin"]          # هیچ فایلی گم یا تکراری نشد
    assert "1" not in tb._sessions
    assert os.listdir(env) == []


@pytest.mark.skipif(not tb.SEVENZIP_BIN, reason="7z نصب نیست")
async def test_encrypted_split_archive_resumes_with_right_mode(env, tmp_path, monkeypatch):
    big = env / "big.bin"
    payload = os.urandom(3 * 1024 * 1024)
    big.write_bytes(payload)
    monkeypatch.setattr(tb, "PART_SIZE_MB", 1)
    monkeypatch.setattr(tb, "SPLIT_THRESHOLD_MB", 2)

    tb._sessions["1"] = {"files": [{"path": str(big), "name": "big.bin"}], "waiting": False}
    delivered = tmp_path / "delivered"
    delivered.mkdir()
    state = {"fail_part2": True, "password": None}

    async def fake_send(uid, path, caption):
        if path.endswith(".002") and state["fail_part2"]:
            raise RuntimeError("boom")
        state["password"] = caption.split("رمز فایل: ")[1].split()[0]
        shutil.copy(path, delivered / os.path.basename(path))

    monkeypatch.setattr(tb, "_send_to_rubika", fake_send)

    await tb.do_send("1", True, FakeQuery(), [], "3MB")      # safe+رمز
    assert not big.exists()                                   # فایل اصلی بعد از آرشیو پاک شده
    s = tb._sessions["1"]
    remaining = len(s["part_files"])
    assert s["sent"] == 1 and remaining >= 2                  # فقط پارت‌های مونده در صف
    total_parts = 1 + remaining

    _restart()
    state["fail_part2"] = False
    # کاربر اشتباهاً «معمولی» رو می‌زنه؛ باید همون آرشیو رمزدار ادامه پیدا کنه
    await tb.do_send("1", False, FakeQuery(), [], "3MB")
    assert sorted(os.listdir(delivered)) == [f"bundle_1.7z.{i:03d}" for i in range(1, total_parts + 1)]

    first = str(delivered / "bundle_1.7z.001")
    # اسم فایل‌ها هم رمزنگاری شده (-mhe=on): بدون رمز درست حتی لیست‌گیری شکست می‌خوره
    bad = subprocess.run([tb.SEVENZIP_BIN, "l", "-pWRONG", first], capture_output=True, text=True)
    assert bad.returncode != 0
    out = tmp_path / "out"
    ok = subprocess.run([tb.SEVENZIP_BIN, "x", f"-p{state['password']}", f"-o{out}", first, "-y"],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    assert (out / "big.bin").read_bytes() == payload


async def test_safe_mode_after_plain_attempt_still_gets_a_password(env, monkeypatch):
    """باگ قبلی: password=None از تلاش قبلی می‌موند و کپشن «رمز: None» می‌شد."""
    files = _make_files(env, ["a.bin"])
    tb._sessions["1"] = {"files": list(files), "waiting": False, "password": None}
    captions = []

    async def fake_send(uid, path, caption):
        captions.append(caption)

    monkeypatch.setattr(tb, "_send_to_rubika", fake_send)
    if tb.SEVENZIP_BIN:
        await tb.do_send("1", True, FakeQuery(), files, "1MB")
        assert captions and "None" not in captions[0]
        pw = captions[0].split("رمز فایل: ")[1].split()[0]
        assert len(pw) == 14


async def test_cancel_interrupts_a_running_upload(env, monkeypatch):
    files = _make_files(env, ["a.bin", "b.bin"])
    tb._sessions["1"] = {"files": list(files), "waiting": False}
    started = asyncio.Event()

    class SlowClient:
        def __init__(self, name=None): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def send_document(self, *a, **k):
            started.set()
            await asyncio.sleep(60)

    monkeypatch.setattr(tb, "Client", SlowClient)
    q = FakeQuery()
    task = asyncio.create_task(tb.do_send("1", False, q, files, "1MB"))
    await asyncio.wait_for(started.wait(), 5)
    tb._cancel_flags["1"] = True
    await asyncio.wait_for(task, 5)                  # باید خیلی زودتر از ۶۰ ثانیه تموم بشه
    assert "لغو شد" in q.texts[-1]
    assert "1" not in tb._sessions
    assert os.listdir(env) == []                      # فایل‌ها پاک شدن


async def test_double_click_does_not_start_two_sends(env, monkeypatch):
    files = _make_files(env, ["a.bin"])
    tb._sessions["1"] = {"files": list(files), "waiting": False, "sending": True}
    called = []

    async def fake_send(uid, path, caption):
        called.append(path)

    monkeypatch.setattr(tb, "_send_to_rubika", fake_send)
    await tb.do_send("1", False, FakeQuery(), files, "1MB")
    assert called == []


@pytest.mark.skipif(not tb.SEVENZIP_BIN, reason="7z نصب نیست")
async def test_fixed_archive_password_is_used_but_never_leaked(env, tmp_path, monkeypatch):
    fixed = "My-Fixed-Pass-123"
    monkeypatch.setattr(tb, "ARCHIVE_PASSWORD", fixed)
    files = _make_files(env, ["a.bin"])
    tb._sessions["1"] = {"files": list(files), "waiting": False}
    got = {}

    async def fake_send(uid, path, caption):
        got["caption"] = caption
        shutil.copy(path, tmp_path / os.path.basename(path))
        # در لحظه‌ی ارسال، فایل session روی دیسک هم نباید رمز رو داشته باشه
        sess_file = session_store.SESSIONS_FILE
        got["disk"] = open(sess_file, encoding="utf-8").read() if os.path.exists(sess_file) else ""

    monkeypatch.setattr(tb, "_send_to_rubika", fake_send)
    await tb.do_send("1", True, FakeQuery(), files, "1MB")

    assert fixed not in got["caption"]                  # توی کپشن روبیکا نیست
    assert fixed not in got["disk"]                     # توی active_sessions.json هم نیست
    assert "password" not in tb._sessions.get("1", {})
    out = tmp_path / "out"
    ok = subprocess.run([tb.SEVENZIP_BIN, "x", f"-p{fixed}", f"-o{out}", str(tmp_path / "bundle_1.7z"), "-y"],
                        capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr    # آرشیو واقعاً با رمز ثابت باز می‌شه
    assert (out / "a.bin").exists()


def test_short_archive_password_is_rejected_at_startup(monkeypatch):
    import importlib, config
    monkeypatch.setenv("ARCHIVE_PASSWORD", "short")
    with pytest.raises(ValueError, match="حداقل 12"):
        importlib.reload(config)
    monkeypatch.delenv("ARCHIVE_PASSWORD")
    importlib.reload(config)                            # وضعیت ماژول رو به حالت عادی برگردون
