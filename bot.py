import logging
import os
import re
import random
import asyncio
import shutil
import tempfile
from collections import defaultdict
from dotenv import load_dotenv
from telegram import Update, ReactionTypeEmoji, ReplyParameters
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

import yt_dlp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()
BOT_TOKEN = os.environ.get("BOT_TOKEN")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")

# ID пользователя которому отвечаем гифкой
TARGET_USER_ID = 5002964279

# file_id гифки
GIF_FILE = "CgACAgIAAxkBAAFD2mlpqH5Qrh_vFdkM_rbmUEJP3sJu6gAC3HYAAkciUEi9sy6F7yG9WToE"

REACTIONS = ["🔥", "👀", "🤡", "💯"]

message_counter: dict[int, int] = defaultdict(int)

URL_PATTERN = re.compile(
    r'https?://(www\.)?(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com|instagram\.com/(reels?|p)/|twitter\.com|x\.com|youtube\.com|youtu\.be)'
)

def is_valid_url(text: str) -> bool:
    return bool(URL_PATTERN.search(text))


def download_video(url: str, tmp_dir: str) -> tuple[str, dict]:
    """Скачивает видео в tmp_dir и возвращает (путь к файлу, info dict)."""
    ydl_opts = {
        'outtmpl': os.path.join(tmp_dir, '%(id)s.%(ext)s'),
        # Ограничение 50 МБ — максимум для Telegram Bot API через reply_video
        'format': 'best[ext=mp4][filesize<50M]/best[filesize<50M]/best',
        'quiet': True,
        'merge_output_format': 'mp4',
        'socket_timeout': 30,
        'noplaylist': True,
        'postprocessors': [{
            'key': 'FFmpegVideoConvertor',
            'preferedformat': 'mp4',
        }],
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        
        info = ydl.extract_info(url, download=False)
        duration = info.get("duration", 0)
        if duration > 600:
            raise ValueError(f"Видео слишком длинное: {duration // 60} мин. Максимум 10 минут.")

        info = ydl.extract_info(url, download=True)

        filename = None
        if "requested_downloads" in info and info["requested_downloads"]:
            filename = info["requested_downloads"][0].get("filepath")

        if not filename:
            filename = ydl.prepare_filename(info)

        if not os.path.exists(filename):
            base = os.path.splitext(filename)[0]
            for ext in ('mp4', 'mkv', 'webm', 'mov'):
                candidate = f"{base}.{ext}"
                if os.path.exists(candidate):
                    filename = candidate
                    break

        if not os.path.exists(filename):
            raise FileNotFoundError(f"Файл не найден после скачивания: {filename}")

        if os.path.getsize(filename) < 1024:
            raise ValueError("Скачанный файл подозрительно маленький (< 1 КБ)")

    return filename, info


async def send_video(filename: str, update: Update, info: dict) -> None:
    """Отправляет видео с метаданными. При ошибке — как документ."""
    reply_params = ReplyParameters(message_id=update.message.message_id)

    duration = int(info.get("duration") or 0)
    width = int(info.get("width") or 0)
    height = int(info.get("height") or 0)

    try:
        with open(filename, 'rb') as f:
            await update.message.reply_video(
                video=f,
                reply_parameters=reply_params,
                supports_streaming=True,
                duration=duration,
                width=width,
                height=height,
            )
    except Exception as e:
        logger.warning(f"reply_video не удался, пробую document: {e}")
        with open(filename, 'rb') as f:
            await update.message.reply_document(
                document=f,
                reply_parameters=reply_params,
            )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    chat_id = update.message.chat_id
    message_counter[chat_id] += 1
    if message_counter[chat_id] >= 150:
        message_counter[chat_id] = 0
        await update.message.reply_text("а я считаю это желтуха")

    # Гифка целевому пользователю с шансом 1% — на любое сообщение, включая медиа
    if update.message.from_user and update.message.from_user.id == TARGET_USER_ID:
        if random.random() < 0.01:
            try:
                await update.message.reply_animation(
                    animation=GIF_FILE,
                    reply_parameters=ReplyParameters(message_id=update.message.message_id),
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить гифку: {e}")

    text = (update.message.text or update.message.caption or "").strip()

    if not text:
        return

    # Реакция на любое сообщение с шансом 4%
    if random.random() < 0.04:
        try:
            await update.message.set_reaction(
                [ReactionTypeEmoji(emoji=random.choice(REACTIONS))]
            )
        except Exception as e:
            logger.warning(f"Не удалось поставить реакцию: {e}")

    if not is_valid_url(text):
        return

    msg = await update.message.reply_text("⏳ Завозик...")
    tmp_dir = tempfile.mkdtemp(prefix="yt_")
    filename = None
    try:
        download_semaphore = context.bot_data.get("download_semaphore")
        async with download_semaphore:
            filename, info = await asyncio.wait_for(
                asyncio.to_thread(download_video, text, tmp_dir),
                timeout=120,
            )

        if filename and os.path.exists(filename):
            await send_video(filename, update, info)
        else:
            logger.error(f"Файл не найден после скачивания: {filename}")
            await msg.edit_text("❌ Не удалось найти скачанный файл.")
            return

    except asyncio.TimeoutError:
        logger.error(f"Таймаут при скачивании [{text}]")
        await msg.edit_text("❌ Скачивание заняло слишком долго, попробуй позже.")
    except Exception as e:
        logger.error(f"Ошибка при скачивании или отправке [{text}]: {e}")
        await msg.edit_text("❌ Не удалось скачать видео.")
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        logger.info(f"Временная папка удалена: {tmp_dir}")
        try:
            await msg.delete()
        except Exception as e:
            logger.warning(f"Не удалось удалить сообщение-статус: {e}")


def main() -> None:
    logger.info("Бот запускается...")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.bot_data["download_semaphore"] = asyncio.Semaphore(3)

    app.add_handler(
        MessageHandler(filters.ALL & ~filters.COMMAND, handle_message)
    )
    print("✅ Бот запущен. Нажми Ctrl+C чтобы остановить.")
    logger.info("Бот запущен")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()