import os
import io
import sys
import json
import asyncio
import logging
import datetime
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.errors import FloodWaitError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument
import TelethonFakeTLS

# ===== 1. НАСТРОЙКА ЛОГИРОВАНИЯ =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("parser.log", encoding='utf-8'),
        logging.StreamHandler(stream=sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# ===== 2. ЗАГРУЗКА ПЕРЕМЕННЫХ ОКРУЖЕНИЯ =====
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
TARGET_CHAT_ID = int(os.getenv("TARGET_CHAT_ID"))

TEMP_DIR = "temp_media"
os.makedirs(TEMP_DIR, exist_ok=True)

# ===== 3. ФАЙЛ СОСТОЯНИЯ =====
STATE_FILE = "last_post_ids.json"

# Лимит Bot API — 50 МБ
MAX_BOT_SIZE = 50 * 1024 * 1024

# Таймаут на скачивание одного медиа (в секундах)
DOWNLOAD_TIMEOUT = 180


def load_last_ids():
    """Загружает сохранённые ID последних обработанных постов из файла."""
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                return {int(k): v for k, v in data.items()}
        except Exception as e:
            logger.warning(f"Не удалось загрузить {STATE_FILE}: {e}. Начинаем с нуля.")
    return {}


def save_last_ids(ids):
    """Сохраняет ID последних обработанных постов в файл."""
    try:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(ids, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Не удалось сохранить {STATE_FILE}: {e}")


# ===== 4. КАНАЛЫ И ДАТА НАЧАЛА (ЖЁСТКО В КОДЕ) =====
RAW_CHANNELS = [-1001461849007]
CHANNELS = RAW_CHANNELS

START_DATE = datetime.datetime(2026, 10, 10, 0, 0, 0, tzinfo=datetime.timezone.utc)

# ===== 5. ЗАГРУЗКА СОСТОЯНИЯ =====
last_post_ids = load_last_ids()
logger.info(f"Загружены ID последних обработанных постов: {last_post_ids}")

# ===== 6. НАСТРОЙКА ПРОКСИ (FakeTLS) =====
proxy = (
    "max.kimt.click",
    443,
    "1b153cf06dbd43c6085c359a6702eb936d61782e6b696d742e636c69636b"
)

# ===== 7. СОЗДАНИЕ КЛИЕНТОВ =====
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
    """Обрезает текст, чтобы вместе с источником он влезал в лимит подписи Telegram."""
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
    """
    Скачивает медиа в память (BytesIO) с таймаутом.
    Возвращает кортеж (BytesIO, filename) или (None, None) при ошибке.
    """
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

        size_hint = ""
        if message.document and message.document.size:
            size_hint = f", ожидаемый размер={message.document.size / (1024 * 1024):.2f} МБ"

        logger.info(f"[DOWNLOAD] Начинаю скачивание id={message.id}, тип={media_type}, имя={filename}{size_hint}")

        try:
            media_bytes = await asyncio.wait_for(
                user_client.download_media(message, file=bytes),
                timeout=DOWNLOAD_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(f"[DOWNLOAD] Таймаут {DOWNLOAD_TIMEOUT}с для id={message.id}. Пропускаем.")
            return None, None

        if not media_bytes:
            logger.warning(f"[DOWNLOAD] Медиа не скачалось (пустой ответ) для id={message.id}")
            return None, None

        size_mb = len(media_bytes) / (1024 * 1024)
        logger.info(f"[DOWNLOAD] Успешно скачано id={message.id}, размер={size_mb:.2f} МБ")

        bytes_io = io.BytesIO(media_bytes)
        bytes_io.seek(0)
        bytes_io.name = filename

        return bytes_io, filename

    except Exception as e:
        logger.error(f"[DOWNLOAD] Ошибка при загрузке медиа (id={message.id}): {type(e).__name__}: {e}", exc_info=True)
        return None, None


async def send_media_safely(msg, files_data, full_caption):
    """
    Отправляет медиа через send_file.
    Если общий размер > 50 МБ — отправляет через user_client.
    """
    try:
        media_type = get_media_type(msg)
        logger.info(f"[SEND] Попытка отправки id={msg.id}, тип={media_type}, файлов={len(files_data)}")

        prepared_files = []
        for f_io, fname in files_data:
            if not hasattr(f_io, 'name') or not f_io.name:
                f_io.name = fname
            prepared_files.append(f_io)

        total_size = sum(f_io.getbuffer().nbytes for f_io in prepared_files)
        total_mb = total_size / (1024 * 1024)

        if total_size > MAX_BOT_SIZE:
            logger.info(f"[SEND] Файл {total_mb:.2f} МБ > 50 МБ, отправляю через user_client")
            client = user_client
            client_name = "user"
        else:
            client = bot_client
            client_name = "bot"

        if len(prepared_files) == 1:
            file_io = prepared_files[0]
            force_doc = (media_type == "document")
            await client.send_file(
                TARGET_CHAT_ID,
                file_io,
                caption=full_caption if full_caption else None,
                force_document=force_doc
            )
        else:
            await client.send_file(
                TARGET_CHAT_ID,
                prepared_files,
                caption=full_caption if full_caption else None
            )

        logger.info(f"[SEND] Успешно отправлен медиа-пост id={msg.id} через {client_name} ({total_mb:.2f} МБ)")
        return True

    except FloodWaitError as e:
        logger.warning(f"[SEND] Flood wait при отправке медиа: {e.seconds} сек")
        await asyncio.sleep(e.seconds)
        return False
    except Exception as e:
        logger.error(f"[SEND] Ошибка при отправке медиа (id={msg.id}): {type(e).__name__}: {e}", exc_info=True)
        return False


async def send_media_via_media_ref(msg, full_caption):
    """
    Отправляет медиа через msg.media напрямую (без скачивания).
    Telegram сам передаёт файл со своих серверов в целевой чат.
    Работает только через user_client.
    """
    try:
        logger.info(f"[MEDIA_REF] Пробую отправить id={msg.id} через msg.media (без скачивания)")
        await user_client.send_file(
            TARGET_CHAT_ID,
            msg.media,
            caption=full_caption if full_caption else None
        )
        logger.info(f"[MEDIA_REF] Успешно отправлено id={msg.id} через msg.media")
        return True
    except Exception as e:
        logger.error(f"[MEDIA_REF] Не удалось отправить id={msg.id} через msg.media: {type(e).__name__}: {e}")
        return False


async def forward_latest_posts():
    while True:
        for channel in CHANNELS:
            try:
                entity = await user_client.get_entity(channel)
                await asyncio.sleep(1)

                last_id = last_post_ids.get(channel, 0)

                # ===== СБОР СООБЩЕНИЙ =====
                messages = []
                async for msg in user_client.iter_messages(entity, min_id=last_id):
                    if msg.date < START_DATE:
                        break
                    messages.append(msg)

                messages.reverse()

                if not messages:
                    logger.info(f"Нет новых сообщений в канале {channel} (last_id={last_id})")
                    continue

                logger.info(f"Найдено {len(messages)} новых сообщений в канале {channel}")

                # ===== ОТСЛЕЖИВАЕМ ОБРАБОТАННЫЕ АЛЬБОМЫ В ЭТОЙ ИТЕРАЦИИ =====
                processed_groups = set()

                for msg in messages:
                    last_id_check = last_post_ids.get(channel, 0)
                    if msg.id <= last_id_check:
                        continue

                    # Если это часть уже обработанного альбома — пропускаем
                    if msg.grouped_id and msg.grouped_id in processed_groups:
                        logger.info(f"[SKIP] id={msg.id} — часть уже обработанного альбома grouped_id={msg.grouped_id}")
                        continue

                    caption = (msg.message or "").strip()
                    source = f"\n\nИсточник: https://t.me/c/{str(channel).replace('-100', '')}/{msg.id}"
                    full_caption = truncate_text(caption, source, 1024)

                    success = False
                    max_id_in_album = msg.id  # сюда запишем максимальный ID альбома

                    if msg.media:
                        media_type = get_media_type(msg)
                        logger.info(f"[MAIN] Обработка id={msg.id}, медиа={media_type}")

                        try:
                            # ==== СБОРКА АЛЬБОМА ====
                            if msg.grouped_id:
                                album = [msg]
                                async for m in user_client.iter_messages(
                                    entity, min_id=msg.id - 20, max_id=msg.id + 20
                                ):
                                    if m.grouped_id == msg.grouped_id and m.id != msg.id:
                                        album.append(m)
                                album.sort(key=lambda x: x.id)
                                logger.info(f"[MAIN] Найден альбом из {len(album)} элементов для id={msg.id}")

                                # Помечаем grouped_id как обработанный в этой итерации
                                processed_groups.add(msg.grouped_id)

                                # Запоминаем максимальный ID альбома
                                max_id_in_album = max(m.id for m in album)
                                logger.info(f"[MAIN] Альбом grouped_id={msg.grouped_id}: max_id={max_id_in_album}")
                            else:
                                album = [msg]

                            # ==== ОПРЕДЕЛЯЕМ РАЗМЕР И РЕШАЕМ, КАК ДЕЙСТВОВАТЬ ====
                            media_size = msg.document.size if (msg.document and msg.document.size) else 0
                            is_large = media_size > MAX_BOT_SIZE

                            if is_large and len(album) == 1:
                                # Большое одиночное медиа — сразу через msg.media
                                size_mb = media_size / (1024 * 1024)
                                logger.info(f"[MAIN] Большое медиа ({size_mb:.2f} МБ), иду напрямую через msg.media")
                                if await send_media_via_media_ref(msg, full_caption):
                                    success = True
                                elif full_caption:
                                    try:
                                        await bot_client.send_message(TARGET_CHAT_ID, full_caption)
                                        success = True
                                    except Exception as e2:
                                        logger.error(f"Не удалось отправить текст: {e2}")
                            else:
                                # Обычный путь: скачиваем
                                files_data = []
                                for m in album:
                                    bio, fname = await download_media_bytes(m)
                                    if bio:
                                        files_data.append((bio, fname))

                                logger.info(f"[MAIN] Скачано файлов: {len(files_data)} из {len(album)}")

                                if files_data:
                                    sent = await send_media_safely(msg, files_data, full_caption)
                                    if sent:
                                        success = True
                                    elif full_caption:
                                        logger.warning(f"[MAIN] Медиа не ушло, отправляю только текст id={msg.id}")
                                        try:
                                            await bot_client.send_message(TARGET_CHAT_ID, full_caption)
                                            success = True
                                        except Exception as e2:
                                            logger.error(f"Не удалось отправить текст: {e2}")

                                # Fallback: если скачать не удалось и это одиночное медиа
                                if not success and not files_data and len(album) == 1:
                                    logger.warning(f"[MAIN] Скачать не удалось, пробую fallback через msg.media id={msg.id}")
                                    if await send_media_via_media_ref(msg, full_caption):
                                        success = True
                                    elif full_caption:
                                        try:
                                            await bot_client.send_message(TARGET_CHAT_ID, full_caption)
                                            success = True
                                        except Exception as e2:
                                            logger.error(f"Не удалось отправить текст: {e2}")

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
                                    success = True
                                except Exception as e2:
                                    logger.error(f"Не удалось отправить текст: {e2}")

                    elif full_caption:
                        try:
                            await bot_client.send_message(TARGET_CHAT_ID, full_caption)
                            logger.info(f"Отправлен текст id={msg.id}")
                            success = True
                        except FloodWaitError as e:
                            logger.warning(f"Flood wait при отправке текста: {e.seconds} сек")
                            await asyncio.sleep(e.seconds)

                    # === СОХРАНЯЕМ ID ТОЛЬКО ПРИ УСПЕХЕ ===
                    if success:
                        # Для альбома сохраняем максимальный ID, чтобы пропустить все его элементы
                        last_post_ids[channel] = max_id_in_album
                        save_last_ids(last_post_ids)
                        logger.info(f"[SAVE] Сохранён last_id={max_id_in_album} для канала {channel}")
                    else:
                        logger.warning(f"[SKIP] Пост id={msg.id} не обработан, ID не сохранён. Повторим при следующем запуске.")

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