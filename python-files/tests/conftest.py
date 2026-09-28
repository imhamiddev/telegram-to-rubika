"""
conftest.py — تنظیمات مشترک pytest برای این پروژه

این پروژه به کتابخانه‌های خارجی (python-telegram-bot، rubpy، pyrogram،
jdatetime، pyzipper) وابسته‌ست که نصبشون برای اجرای تست‌های واحد لازم نیست
(چون این تست‌ها فقط منطق خالص پایتون رو چک می‌کنن، نه ارتباط واقعی با
تلگرام/روبیکا). برای این‌که تست‌ها بدون نیاز به نصب کامل requirements.txt
هم قابل اجرا باشن، این فایل قبل از هر import از ماژول‌های پروژه، اگه
کتابخونه‌ی واقعی نصب نبود، یک نسخه‌ی mock سبک جایگزینش می‌کنه.

اگه کتابخونه‌های واقعی نصب باشن (حالت عادی توسعه، با pip install -r
requirements.txt)، این فایل هیچ کاری نمی‌کنه و از نسخه‌ی واقعی استفاده
می‌شه؛ mock فقط زمانی جایگزین می‌شه که import واقعی شکست بخوره.
"""

import os
import sys
import types
import importlib
import tempfile

# مسیر python-files رو به sys.path اضافه می‌کنیم تا import مستقیم ماژول‌های
# پروژه (config, telegram_bot, downloader, ...) کار کنه.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)


def _install_mock(module_name: str, builder):
    """اگه ماژول واقعی import نشد، نسخه‌ی mock رو در sys.modules می‌ذاره."""
    try:
        importlib.import_module(module_name)
    except ImportError:
        sys.modules[module_name] = builder()


class _FlexibleMock:
    """
    جایگزین کلاس‌های PTB مثل InlineKeyboardButton/KeyboardButton که در کد
    اصلی با آرگومان‌های گوناگون (از جمله style=، api_kwargs=) صدا زده
    می‌شن. هر آرگومانی رو بدون خطا قبول می‌کنه.
    """
    def __init__(self, *args, **kwargs):
        self.args = args
        self.kwargs = kwargs

    def __repr__(self):
        return f"{self.__class__.__name__}(args={self.args!r}, kwargs={self.kwargs!r})"


def _build_telegram_mock():
    mod = types.ModuleType("telegram")
    for name in ["Update", "InlineKeyboardButton", "InlineKeyboardMarkup",
                 "ReplyKeyboardMarkup", "KeyboardButton", "ReactionTypeEmoji"]:
        setattr(mod, name, type(name, (_FlexibleMock,), {}))
    return mod


def _build_telegram_ext_mock():
    mod = types.ModuleType("telegram.ext")

    class _FakeAppBuilder:
        def token(self, t):
            return self

        def post_init(self, cb):
            self._post_init = cb
            return self

        def build(self):
            app = types.SimpleNamespace()
            app.post_init_cb = getattr(self, "_post_init", None)
            app.add_handler = lambda *a, **k: None

            async def _fake_send_message(**k):
                return None
            app.bot = types.SimpleNamespace(send_message=_fake_send_message)
            app.run_polling = lambda *a, **k: None
            return app

    mod.ApplicationBuilder = _FakeAppBuilder
    for name in ["MessageHandler", "CallbackQueryHandler", "CommandHandler"]:
        setattr(mod, name, type(name, (), {}))
    mod.filters = types.SimpleNamespace(
        Document=types.SimpleNamespace(ALL=None),
        VIDEO=None, AUDIO=None, PHOTO=None, VOICE=None,
        TEXT=None, COMMAND=None,
    )
    mod.ContextTypes = types.SimpleNamespace(DEFAULT_TYPE=None)
    return mod


def _build_rubpy_mock():
    mod = types.ModuleType("rubpy")
    mod.Client = type("Client", (), {})
    return mod


def _build_pyrogram_mock():
    mod = types.ModuleType("pyrogram")
    mod.Client = type("Client", (), {})
    return mod


def _build_jdatetime_mock():
    mod = types.ModuleType("jdatetime")

    class _FakeDatetime:
        @staticmethod
        def now():
            return types.SimpleNamespace(year=1404, month=7, day=4, hour=12, minute=30)

    mod.datetime = _FakeDatetime
    return mod


def _build_pyzipper_mock():
    mod = types.ModuleType("pyzipper")
    mod.AESZipFile = type("AESZipFile", (), {})
    mod.ZIP_DEFLATED = 8
    mod.WZ_AES = 99
    return mod


_install_mock("telegram", _build_telegram_mock)
_install_mock("telegram.ext", _build_telegram_ext_mock)
_install_mock("rubpy", _build_rubpy_mock)
_install_mock("pyrogram", _build_pyrogram_mock)
_install_mock("jdatetime", _build_jdatetime_mock)
_install_mock("pyzipper", _build_pyzipper_mock)


# ─── فیکسچرهای مشترک ────────────────────────────────────────────
# import pytest اختیاریه: خود اجرای عادی تست‌ها (با دستور "pytest") همیشه
# pytest نصب‌شده رو در دسترس داره، ولی این فایل توسط ابزارهای کمکی دیگه
# (مثل یک اسکریپت بررسی سریع) هم import می‌شه که لزوماً pytest رو ندارن؛
# در اون حالت صرفاً از تعریف فیکسچر isolated_env صرف‌نظر می‌کنیم.
try:
    import pytest

    @pytest.fixture
    def isolated_env(tmp_path, monkeypatch):
        """
        یک پوشه‌ی موقت و کاملاً ایزوله برای هر تست فراهم می‌کنه (دانلود،
        دیتابیس آمار، فایل session) تا تست‌ها روی فایل‌های واقعی پروژه اثر
        نذارن و از هم مستقل باشن. env varهای مربوطه رو هم قبل از import
        ماژول‌های پروژه ست می‌کنه.
        """
        monkeypatch.setenv("DOWNLOAD_DIR", str(tmp_path / "downloads"))
        monkeypatch.setenv("ALLOWED_USER_ID", "12345")
        monkeypatch.setenv("TELEGRAM_TOKEN", "fake-token")
        monkeypatch.setenv("MAX_SIZE_MB", "2000")
        return tmp_path

except ImportError:
    pass
