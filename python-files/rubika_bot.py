import asyncio
import os
import secrets
import string
import math
import jdatetime
import pyzipper
from rubpy import Client

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RUBIKA_SESSION = os.path.join(BASE_DIR, 'my_session')
RUBIKA_CHAT_ID = 'me'

SPLIT_THRESHOLD_MB = 100   # بالای این تقسیم میشه
PART_SIZE_MB = 95          # حجم هر پارت

def get_shamsi_caption():
    now = jdatetime.datetime.now()
    return f"{now.year}/{now.month:02d}/{now.day:02d} - {now.hour:02d}:{now.minute:02d}"

def generate_password(length=14):
    # فقط حروف و عدد - بدون کاراکتر خاص که ممکنه توی بعضی برنامه‌ها مشکل بده
    chars = string.ascii_letters + string.digits
    return ''.join(secrets.choice(chars) for _ in range(length))

def random_filename(original_filename):
    ext = os.path.splitext(original_filename)[1]
    return secrets.token_hex(10) + ext

def cleanup(*paths):
    for p in paths:
        try:
            if p and os.path.exists(p):
                os.remove(p)
        except Exception:
            pass

def create_aes_zip(file_path, password):
    """
    زیپ رمزدار با AES-256 واقعی برای فایل‌های زیر ۱۰۰MB
    برخلاف zip -P که ZipCrypto قدیمی و خطاپذیر استفاده میکنه،
    pyzipper از AES-256 استفاده میکنه - هم امن‌تره هم بدون checksum error
    """
    zip_path = file_path + ".zip"
    with pyzipper.AESZipFile(
        zip_path,
        mode='w',
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES
    ) as zf:
        zf.setpassword(password.encode('utf-8'))
        # فقط نام فایل داخل زیپ - بدون مسیر کامل
        zf.write(file_path, arcname=os.path.basename(file_path))
    return [zip_path]

def create_split_aes_zip(file_path, password, part_size_mb):
    """
    زیپ رمزدار AES-256 + تقسیم به پارت برای فایل‌های بالای ۱۰۰MB
    ابتدا یک زیپ کامل میسازیم، بعد با Python خودمون split میکنیم
    (قابل جمع: cat part1 part2 > full.zip  یا  copy /b part1+part2 full.zip)
    """
    # مرحله ۱: ساخت زیپ کامل
    full_zip = file_path + ".zip"
    with pyzipper.AESZipFile(
        full_zip,
        mode='w',
        compression=pyzipper.ZIP_DEFLATED,
        encryption=pyzipper.WZ_AES
    ) as zf:
        zf.setpassword(password.encode('utf-8'))
        zf.write(file_path, arcname=os.path.basename(file_path))

    # مرحله ۲: تقسیم زیپ به پارت‌های مساوی
    part_size_bytes = part_size_mb * 1024 * 1024
    total_size = os.path.getsize(full_zip)
    total_parts = math.ceil(total_size / part_size_bytes)

    part_files = []
    with open(full_zip, 'rb') as src:
        for i in range(1, total_parts + 1):
            part_path = f"{file_path}_part{i:03d}.zip"
            chunk = src.read(part_size_bytes)
            if not chunk:
                break
            with open(part_path, 'wb') as dst:
                dst.write(chunk)
            part_files.append(part_path)

    # حذف زیپ کامل که دیگه لازم نیست
    cleanup(full_zip)

    if not part_files:
        raise Exception("فایل‌های پارت ساخته نشدن")
    return part_files

async def _send_async(file_path, original_filename, safe_mode=False, progress_callback=None):
    password = None
    part_files = []
    caption = get_shamsi_caption()
    file_size_mb = os.path.getsize(file_path) / (1024 * 1024)

    try:
        is_split = safe_mode and file_size_mb >= SPLIT_THRESHOLD_MB

        if safe_mode:
            password = generate_password()
            if is_split:
                if progress_callback:
                    await progress_callback(
                        f"🔐 در حال ساخت ZIP رمزدار AES-256\n"
                        f"({int(file_size_mb)}MB → پارت‌های {PART_SIZE_MB}MB)..."
                    )
                part_files = create_split_aes_zip(file_path, password, PART_SIZE_MB)
            else:
                if progress_callback:
                    await progress_callback("🔐 در حال ساخت ZIP رمزدار AES-256...")
                part_files = create_aes_zip(file_path, password)
        else:
            part_files = [file_path]

        total_parts = len(part_files)

        async with Client(name=RUBIKA_SESSION) as client:
            for i, part_path in enumerate(part_files, 1):
                part_name = os.path.basename(part_path)
                part_size = os.path.getsize(part_path) / (1024 * 1024)

                if progress_callback:
                    if total_parts > 1:
                        await progress_callback(
                            f"📤 آپلود پارت {i} از {total_parts}\n"
                            f"📁 {part_name} ({part_size:.1f}MB)"
                        )
                    else:
                        await progress_callback("📤 در حال آپلود به روبیکا...")

                part_caption = caption
                if total_parts > 1:
                    part_caption = (
                        f"{caption}\n"
                        f"📦 پارت {i}/{total_parts} — {original_filename}\n"
                        f"⚠️ برای استخراج: همه پارت‌ها رو کنار هم بذار،\n"
                        f"بعد part001.zip رو باز کن"
                    )

                await client.send_document(
                    RUBIKA_CHAT_ID,
                    part_path,
                    caption=part_caption,
                )

        if progress_callback:
            if total_parts > 1:
                await progress_callback(f"✅ همه {total_parts} پارت آپلود شدن!")
            else:
                await progress_callback("✅ آپلود کامل شد!")

    finally:
        cleanup(file_path)
        for p in part_files:
            if p != file_path:
                cleanup(p)

    return password, len(part_files)

# ─── Queue ───────────────────────────────────────────────────────────────────

_queue = asyncio.Queue()
_worker_started = False

async def _worker():
    while True:
        task = await _queue.get()
        file_path, original_filename, safe_mode, progress_cb, result_future = task
        try:
            result = await _send_async(file_path, original_filename, safe_mode, progress_cb)
            result_future.set_result(result)
        except Exception as e:
            cleanup(file_path)
            result_future.set_exception(e)
        finally:
            _queue.task_done()

def _ensure_worker(loop):
    global _worker_started
    if not _worker_started:
        loop.create_task(_worker())
        _worker_started = True

async def send_to_rubika_async(file_path, original_filename, safe_mode=False, progress_callback=None):
    loop = asyncio.get_event_loop()
    _ensure_worker(loop)

    pos = _queue.qsize()
    if pos > 0 and progress_callback:
        await progress_callback(f"⏳ در صف ارسال... ({pos} فایل جلوتر)")

    result_future = loop.create_future()
    await _queue.put((file_path, original_filename, safe_mode, progress_callback, result_future))
    return await result_future