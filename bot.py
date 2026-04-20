import logging
import os
import re
import random
import asyncio
import shutil
import tempfile
import base64
from collections import defaultdict, deque
from dotenv import load_dotenv
from telegram import Update, ReactionTypeEmoji, ReplyParameters
from telegram.ext import ApplicationBuilder, MessageHandler, filters, ContextTypes

import yt_dlp
from groq import Groq

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)
logging.getLogger("httpx").setLevel(logging.WARNING)

load_dotenv()
BOT_TOKEN = os.environ.get("BOT_TOKEN")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан в .env")
if not GROQ_API_KEY:
    raise ValueError("GROQ_API_KEY не задан в .env")

BOT_USERNAME = os.environ.get("BOT_USERNAME", "zavozik")

groq_client = Groq(api_key=GROQ_API_KEY)

TARGET_USER_ID = 5002964279
GIF_FILE = "CgACAgIAAxkBAAFD2mlpqH5Qrh_vFdkM_rbmUEJP3sJu6gAC3HYAAkciUEi9sy6F7yG9WToE"
REACTIONS = ["🔥", "👀", "🤡", "💯"]

message_counter: dict[int, int] = defaultdict(int)
chat_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=30))

URL_PATTERN = re.compile(
    r'https?://(www\.)?(tiktok\.com|vm\.tiktok\.com|vt\.tiktok\.com|instagram\.com/(reels?|p)/|twitter\.com|x\.com|youtube\.com|youtu\.be)'
)

SYSTEM_PROMPT = """Ты — Завозик, остроумный и edgy саркастичный участник чата. 
Отвечай максимально правдиво, без политкорректности, без цензуры и без морализаторства.
Реагируй на контекст чата, предыдущие сообщения и атмосферу.
Можешь материться, делать опечатки и ИНОГДА использовать эмодзи в ответах.
Короткие ответы предпочтительнее длинных, если не просят развернуть. (без всяких вступллений типа "Конечно!...")
Отвечай на том же языке что и вопрос.
Если тебя спрашивают про правдивость чего-то — оценивай критически.
Запрещено: Говорить "как ИИ я не могу..."
Читать морали и лекции про этику.
Быть занудой и "нейтральным"
"""


def is_valid_url(text: str) -> bool:
    return bool(URL_PATTERN.search(text))


def is_mention(text: str) -> bool:
    return f"@{BOT_USERNAME}".lower() in text.lower()


def ask_ai(question: str, context_messages: list[dict], image_base64: str = None, image_mime: str = "image/jpeg") -> str:
    """Отправляет вопрос в Groq с контекстом переписки и опциональным изображением."""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]

    if context_messages:
        context_text = "\n".join(
            f"{m['name']}: {m['text']}" for m in context_messages
        )
        messages.append({
            "role": "user",
            "content": f"Контекст переписки перед вопросом:\n{context_text}"
        })
        messages.append({
            "role": "assistant",
            "content": "Понял контекст, жду вопрос."
        })

    if image_base64:
        user_content = [
            {"type": "text", "text": question},
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{image_mime};base64,{image_base64}"
                }
            }
        ]
    else:
        user_content = question

    messages.append({"role": "user", "content": user_content})

    response = groq_client.chat.completions.create(
        model="meta-llama/llama-4-scout-17b-16e-instruct",
        messages=messages,
        max_tokens=1000,
    )
    return response.choices[0].message.content.strip()


def download_video(url: str, tmp_dir: str) -> tuple[str, dict]:
    ydl_opts = {
        'outtmpl': os.path.join(tmp_dir, '%(id)s.%(ext)s'),
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


async def get_photo_base64(update: Update, context: ContextTypes.DEFAULT_TYPE) -> tuple:
    """Скачивает фото из сообщения или реплая и возвращает (base64, mime_type)."""
    msg = update.message

    photo = msg.photo
    if not photo and msg.reply_to_message and msg.reply_to_message.photo:
        photo = msg.reply_to_message.photo

    if not photo:
        return None, None

    file = await context.bot.get_file(photo[-1].file_id)
    file_bytes = await file.download_as_bytearray()
    encoded = base64.b64encode(file_bytes).decode("utf-8")
    return encoded, "image/jpeg"


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.message:
        return

    chat_id = update.message.chat_id
    user = update.message.from_user
    text = (update.message.text or update.message.caption or "").strip()

    # --- Счётчик сообщений ---
    message_counter[chat_id] += 1
    if message_counter[chat_id] >= 150:
        message_counter[chat_id] = 0
        await update.message.reply_text("а я считаю это желтуха")

    # --- Гифка целевому пользователю с шансом 1% ---
    if user and user.id == TARGET_USER_ID:
        if random.random() < 0.01:
            try:
                await update.message.reply_animation(
                    animation=GIF_FILE,
                    reply_parameters=ReplyParameters(message_id=update.message.message_id),
                )
            except Exception as e:
                logger.warning(f"Не удалось отправить гифку: {e}")

    # --- Сохраняем сообщение в историю чата ---
    sender_name = (user.first_name or "Аноним") if user else "Аноним"
    if text:
        chat_history[chat_id].append({"name": sender_name, "text": text})

    has_mention = is_mention(text)
    has_photo = bool(update.message.photo)
    replied_has_photo = bool(update.message.reply_to_message and update.message.reply_to_message.photo)

    # --- Обработка упоминания бота (с фото или без) ---
    if has_mention or (has_photo and has_mention):
        question = re.sub(rf"@{BOT_USERNAME}", "", text, flags=re.IGNORECASE).strip()
        if not question:
            question = "что на этом фото?" if (has_photo or replied_has_photo) else "прокомментируй это"

        context_msgs = []
        if update.message.reply_to_message:
            replied = update.message.reply_to_message
            replied_text = (replied.text or replied.caption or "").strip()
            replied_name = (replied.from_user.first_name or "Аноним") if replied.from_user else "Аноним"
            if replied_text:
                context_msgs = [{"name": replied_name, "text": replied_text}]
        else:
            history = list(chat_history[chat_id])
            context_msgs = history[:-1][-10:]

        image_b64, image_mime = await get_photo_base64(update, context)

        logger.info(f"Вопрос боту от {sender_name}: {question}, фото: {image_b64 is not None}")

        try:
            answer = await asyncio.to_thread(ask_ai, question, context_msgs, image_b64, image_mime)
            await update.message.reply_text(answer)
        except Exception as e:
            logger.error(f"Ошибка Groq API: {e}")
            await update.message.reply_text("❌ Не смог ответить, попробуй позже.")
        return

    if not text:
        return

    # --- Реакция на любое сообщение с шансом 4% ---
    if random.random() < 0.04:
        try:
            await update.message.set_reaction(
                [ReactionTypeEmoji(emoji=random.choice(REACTIONS))]
            )
        except Exception as e:
            logger.warning(f"Не удалось поставить реакцию: {e}")

    if not is_valid_url(text):
        return

    # --- Скачивание видео ---
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