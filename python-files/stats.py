import sqlite3
import os
import time

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_stats.db")

def _conn():
    return sqlite3.connect(DB_PATH)

def init_db():
    with _conn() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS events (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                ts        REAL    NOT NULL,
                event     TEXT    NOT NULL,
                size_mb   REAL    DEFAULT 0,
                count     INTEGER DEFAULT 1
            )
        """)
        con.commit()

# ─── ثبت رویدادها ──────────────────────────────────────────────

def log_download(size_mb: float):
    """دانلود فایل از تلگرام"""
    with _conn() as con:
        con.execute("INSERT INTO events (ts, event, size_mb) VALUES (?, 'download', ?)",
                    (time.time(), size_mb))
        con.commit()

def log_send(size_mb: float):
    """ارسال فایل به روبیکا"""
    with _conn() as con:
        con.execute("INSERT INTO events (ts, event, size_mb) VALUES (?, 'send', ?)",
                    (time.time(), size_mb))
        con.commit()

def log_archive(size_mb: float, parts: int = 1):
    """ساخت آرشیو 7z یا زیپ"""
    with _conn() as con:
        con.execute("INSERT INTO events (ts, event, size_mb, count) VALUES (?, 'archive', ?, ?)",
                    (time.time(), size_mb, parts))
        con.commit()

# ─── خواندن آمار ───────────────────────────────────────────────

def _since(days: int) -> float:
    return time.time() - days * 86400

def get_stats(days: int) -> dict:
    since = _since(days)
    with _conn() as con:
        def q(event, col="size_mb"):
            row = con.execute(
                f"SELECT COUNT(*), SUM({col}) FROM events WHERE event=? AND ts>=?",
                (event, since)
            ).fetchone()
            return (row[0] or 0, row[1] or 0.0)

        dl_cnt,  dl_mb   = q("download")
        snd_cnt, snd_mb  = q("send")
        arc_cnt, arc_mb  = q("archive")
        arc_parts        = con.execute(
            "SELECT SUM(count) FROM events WHERE event='archive' AND ts>=?", (since,)
        ).fetchone()[0] or 0

    return {
        "download":  {"count": dl_cnt,  "gb": dl_mb  / 1024},
        "send":      {"count": snd_cnt, "gb": snd_mb  / 1024},
        "archive":   {"count": arc_cnt, "gb": arc_mb  / 1024, "parts": arc_parts},
    }

def format_stats_text() -> str:
    lines = ["📊 آمار ربات\n" + "─" * 22]

    for days, label in [(1, "امروز"), (7, "۷ روز گذشته"), (30, "۳۰ روز گذشته")]:
        s = get_stats(days)
        dl  = s["download"]
        snd = s["send"]
        arc = s["archive"]
        lines.append(
            f"\n📅 {label}:\n"
            f"  ⬇️ دانلود:  {dl['count']} فایل — {dl['gb']:.2f} GB\n"
            f"  📤 ارسال:   {snd['count']} فایل — {snd['gb']:.2f} GB\n"
            f"  🗜 آرشیو:   {arc['count']} بار — {arc['parts']} پارت"
        )

    return "\n".join(lines)

init_db()