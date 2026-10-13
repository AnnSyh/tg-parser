import os
import io
import sys
import asyncio
import logging
import datetime
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
import TelethonFakeTLS

# ===== НАСТРОЙКА ЛОГИРОВАНИЯ =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("parser.log", encoding='utf-8'),
        logging.StreamHandler(stream=sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))

TEMP_DIR = "temp_media"
os.makedirs(TEMP_DIR, exist_ok=True)

RAW_CHANNELS = [-1001461849007]
CHANNELS = RAW_CHANNELS

START_DATE = datetime.datetime(2026, 10, 10, 0, 0, 0, tzinfo=datetime.timezone.utc)
last_post_ids = {}

# ===== НАСТРОЙКА ПРОКСИ =====
proxy = (
    "max.kimt.click",
    443,
    "1b153cf06dbd43c6085c359a6702eb936d61782e6b696d742e636c69636b"
)

# ===== СОЗДАНИЕ КЛИЕНТОВ =====
user_client = TelegramClient(
    "YOUR_SESSION_NAME",
    api_id=API_ID,
    api_hash=API_HASH,
    connection=TelethonFakeTLS.ConnectionTcpMTProxyFakeTLS,
    proxy=proxy,
    timeout=30,
    connection_retries=10,
    retry_delay=2
)

bot_client = TelegramClient(
    "bot_session",
    api_id=API_ID,
    api_hash=API_HASH,
    connection=TelethonFakeTLS.ConnectionTcpMTProxyFakeTLS,
    proxy=proxy,
    timeout=30,
    connection_retries=10,
    retry_delay=2
)


def truncate_text(text, source, max_length=1024):
    if len(text) + len(source) <= max_length:
        return text + source
    available_length = max_length - len(source) - 3
    return text[:available_length] + "..." + source


def get_media_type(message):
    """Определяет тип медиа в сообщении."""
    if not message.media:
        return "none"
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"
    if isinstance(message.media, MessageMediaDocument):
        if message.video:
            return "video"
        return "document"
    return "other"


async def download_media_bytes(message):
    """Скачивает медиа в память (BytesIO)."""
    try:
        media_type = get_media_type(message)
        
        ext = ".bin"
        if media_type == "photo":
            ext = ".jpg"
        elif media_type == "video":
            ext = ".mp4"
        elif media_type == "document" and message.document and message.document.file_name:
            ext = os.path.splitext(message.document.file_name)[1] or ".bin"

        filename = f"media_{message.id}{ext}"
        
        logger.info(f"[DOWNLOAD] Начинаю скачивание id={message.id}, тип={media_type}, имя={filename}")

        media_bytes = await user_client.download_media(message, file=bytes)
        
        if not media_bytes:
            logger.warning(f"[DOWNLOAD] Медиа не скачалось (пустой ответ) для id={message.id}")
            return None, None

        size_mb = len(media_bytes) / (1024 * 1024)
        logger.info(f"[DOWNLOAD] Успешно скачано id={message.id}, размер={size_mb:.2f} МБ")

        bytes_io = io.BytesIO(media_bytes)
        bytes_io.seek(0)
        bytes_io.name = filename  # ВАЖНО: имя нужно для правильного определения типа
        
        return bytes_io, filename

    except Exception as e:
        logger.error(f"[DOWNLOAD] Ошибка при загрузке медиа (id={message.id}): {type(e).__name__}: {e}", exc_info=True)
        return None, None


async def send_media_safely(msg, files_data, full_caption):
    """
    Отправляет медиа, используя универсальный send_file.
    Telethon сам определит тип файла (фото/видео/документ) по имени и содержимому.
    """
    try:
        media_type = get_media_type(msg)
        logger.info(f"[SEND] Попытка отправки id={msg.id}, тип={media_type}, файлов={len(files_data)}")

        prepared_files = []
        for f_io, fname in files_data:
            if not hasattr(f_io, 'name') or not f_io.name:
                f_io.name = fname
            prepared_files.append(f_io)

        if len(prepared_files) == 1:
            file_io = prepared_files[0]
            # Для документов (не фото/видео) отправляем как документ
            # Для фото/видео force_document=False, чтобы Telethon применил сжатие/превью
            force_doc = (media_type == "document")
            
            await bot_client.send_file(
                TARGET_CHAT_ID,
                file_io,
                caption=full_caption if full_caption else None,
                force_document=force_doc
            )
        else:
            # Альбом
            await bot_client.send_file(
                TARGET_CHAT_ID,
                prepared_files,
                caption=full_caption if full_caption else None
            )
            
        logger.info(f"[SEND] Успешно отправлен медиа-пост id={msg.id}")
        return True

    except FloodWaitError as e:
        logger.warning(f"[SEND] Flood wait при отправке медиа: {e.seconds} сек")
        await asyncio.sleep(e.seconds)
        return False
    except Exception as e:
        logger.error(f"[SEND] Ошибка при отправке медиа (id={msg.id}): {type(e).__name__}: {e}", exc_info=True)
        return False


async def forward_latest_posts():
    while True:
        for channel in CHANNELS:
            try:
                entity = await user_client.get_entity(channel)
                await asyncio.sleep(1)

                messages = []
                async for msg in user_client.iter_messages(entity):
                    if msg.date < START_DATE:
                        break
                    messages.append(msg)

                messages.reverse()
                logger.info(f"Найдено {len(messages)} сообщений в канале {channel}")

                for msg in messages:
                    last_id = last_post_ids.get(channel, 0)
                    if msg.id <= last_id:
                        continue
                    last_post_ids[channel] = msg.id

                    caption = (msg.message or "").strip()
                    source = f"\n\nИсточник: https://t.me/c/{str(channel).replace('-100', '')}/{msg.id}"
                    full_caption = truncate_text(caption, source, 1024)

                    if msg.media:
                        media_type = get_media_type(msg)
                        logger.info(f"[MAIN] Обработка id={msg.id}, медиа={media_type}")
                        
                        try:
                            # Собираем альбом
                            if msg.grouped_id:
                                album = [msg]
                                async for m in user_client.iter_messages(
                                    entity, min_id=msg.id - 20, max_id=msg.id + 20
                                ):
                                    if m.grouped_id == msg.grouped_id and m.id != msg.id:
                                        album.append(m)
                                album.sort(key=lambda x: x.id)
                                logger.info(f"[MAIN] Найден альбом из {len(album)} элементов")
                            else:
                                album = [msg]

                            files_data = []
                            for m in album:
                                bio, fname = await download_media_bytes(m)
                                if bio:
                                    files_data.append((bio, fname))

                            logger.info(f"[MAIN] Скачано файлов: {len(files_data)} из {len(album)}")

                            if files_data:
                                success = await send_media_safely(msg, files_data, full_caption)
                                if not success and full_caption:
                                    logger.warning(f"[MAIN] Медиа не ушло, отправляю только текст id={msg.id}")
                                    try:
                                        await bot_client.send_message(TARGET_CHAT_ID, full_caption)
                                    except Exception as e2:
                                        logger.error(f"Не удалось отправить текст: {e2}")
                            else:
                                logger.warning(f"[MAIN] Не удалось скачать медиа id={msg.id}, отправляю только текст")
                                if full_caption:
                                    await bot_client.send_message(TARGET_CHAT_ID, full_caption)

                            # Закрываем BytesIO
                            for f_io, _ in files_data:
                                try:
                                    f_io.close()
                                except Exception:
                                    pass

                        except Exception as e:
                            logger.error(f"[MAIN] Ошибка при обработке медиа id={msg.id}: {type(e).__name__}: {e}", exc_info=True)
                            if full_caption:
                                try:
                                    await bot_client.send_message(TARGET_CHAT_ID, full_caption)
                                except Exception as e2:
                                    logger.error(f"Не удалось отправить текст: {e2}")

                    elif full_caption:
                        try:
                            await bot_client.send_message(TARGET_CHAT_ID, full_caption)
                            logger.info(f"Отправлен текст id={msg.id}")
                        except FloodWaitError as e:
                            logger.warning(f"Flood wait: {e.seconds} сек")
                            await asyncio.sleep(e.seconds)

            except FloodWaitError as e:
                logger.warning(f"Flood wait: {e.seconds} сек")
                await asyncio.sleep(e.seconds)
            except Exception as e:
                logger.error(f"Ошибка при обработке канала {channel}: {type(e).__name__}: {e}", exc_info=True)

        logger.info("Цикл завершён, ожидание 300 секунд")
        await asyncio.sleep(300)


async def main():
    try:
        logger.info("Запуск парсера Telegram (Telethon + FakeTLS)")
        
        try:
            await user_client.start()
            logger.info("user_client успешно авторизован")
        except Exception as e:
            logger.error(f"Критическая ошибка user_client: {e}")
            return
            
        try:
            await bot_client.start(bot_token=BOT_TOKEN)
            logger.info("bot_client успешно авторизован")
        except Exception as e:
            logger.error(f"Критическая ошибка bot_client: {e}")
            return
            
        logger.info("Оба клиента авторизованы. Начинаем работу.")
        await forward_latest_posts()
        
    except KeyboardInterrupt:
        logger.info("Парсер остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())