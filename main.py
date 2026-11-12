import os
import io
import sys
import json
import random
import asyncio
import logging
import datetime
from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import MemorySession
from telethon.errors import FloodWaitError, FilePartMissingError
from telethon.tl.types import MessageMediaPhoto, MessageMediaDocument

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

# ===== 3. ФАЙЛЫ И КОНСТАНТЫ =====
STATE_FILE = "last_post_ids.json"
PROXY_FILE = "socks5.txt"
MAX_BOT_SIZE = 50 * 1024 * 1024
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


# ===== 4. КАНАЛЫ И ДАТА НАЧАЛА =====
RAW_CHANNELS = [-1001461849007]
CHANNELS = RAW_CHANNELS
START_DATE = datetime.datetime(2026, 10, 10, 0, 0, 0, tzinfo=datetime.timezone.utc)
last_post_ids = load_last_ids()


# ===== 5. АВТОМАТИЧЕСКАЯ ПРОВЕРКА И ПОИСК ПРОКСИ =====
async def test_proxy(proxy_tuple):
    """Быстрая проверка одного прокси."""
    # Используем MemorySession, чтобы не создавать лишние файлы на диске
    client = TelegramClient(
        MemorySession(),
        api_id=API_ID,
        api_hash=API_HASH,
        proxy=proxy_tuple,
        connection_retries=0, # Всего 1 попытка
        timeout=5             # Ждем не больше 5 секунд
    )
    try:
        await client.connect()
        await client.disconnect()
        return True
    except Exception:
        return False


async def find_working_proxy():
    """Читает socks5.txt и ищет рабочий прокси пачками (параллельно)."""
    if not os.path.exists(PROXY_FILE):
        logger.error(f"Файл {PROXY_FILE} не найден! Скачайте список прокси и положите его в папку со скриптом.")
        return None

    with open(PROXY_FILE, "r", encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip()]

    # Перемешиваем список, чтобы не долбить одни и те же мертвые IP из начала файла
    random.shuffle(lines)

    logger.info(f"Загружено {len(lines)} прокси. Начинаем параллельную проверку пачками по 30 штук...")

    # Отключаем спам в консоль от Telethon во время тестирования
    logging.getLogger('telethon').setLevel(logging.CRITICAL)

    chunk_size = 30
    chunks = [lines[i:i + chunk_size] for i in range(0, len(lines), chunk_size)]

    for chunk_idx, chunk in enumerate(chunks, 1):
        if chunk_idx % 5 == 0:
            logger.info(f"Проверка пачки {chunk_idx} из {len(chunks)}...")
        
        tasks = []
        valid_proxies = []
        
        for line in chunk:
            try:
                parts = line.split(":")
                if len(parts) == 2:
                    ip, port = parts[0], int(parts[1])
                    proxy_tuple = ("socks5", ip, port)
                    valid_proxies.append(proxy_tuple)
                    tasks.append(test_proxy(proxy_tuple))
            except Exception:
                continue
        
        if not tasks:
            continue

        # Запускаем все задачи в пачке одновременно
        results = await asyncio.gather(*tasks)
        
        # Ищем первый успешный результат
        for i, is_working in enumerate(results):
            if is_working:
                working_proxy = valid_proxies[i]
                logger.info(f"✅ Найден рабочий прокси: {working_proxy[1]}:{working_proxy[2]}")
                # Возвращаем логгеру нормальный уровень
                logging.getLogger('telethon').setLevel(logging.INFO)
                return working_proxy

    logger.error("❌ Ни один прокси из списка не оказался рабочим.")
    logging.getLogger('telethon').setLevel(logging.INFO)
    return None


# ===== 6. ЛОГИКА СКАЧИВАНИЯ И ОТПРАВКИ =====
def truncate_text(text, source, max_length=1024):
    if len(text) + len(source) <= max_length:
        return text + source
    available_length = max_length - len(source) - 3
    return text[:available_length] + "..." + source


def get_media_type(message):
    if not message.media:
        return "none"
    if isinstance(message.media, MessageMediaPhoto):
        return "photo"
    if isinstance(message.media, MessageMediaDocument):
        if message.video:
            return "video"
        return "document"
    return "other"


async def download_media_bytes(user_client, message):
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
        logger.info(f"[DOWNLOAD] Скачивание id={message.id}, тип={media_type}")

        try:
            media_bytes = await asyncio.wait_for(
                user_client.download_media(message, file=bytes),
                timeout=DOWNLOAD_TIMEOUT
            )
        except asyncio.TimeoutError:
            logger.error(f"[DOWNLOAD] Таймаут для id={message.id}")
            return None, None

        if not media_bytes:
            return None, None

        bytes_io = io.BytesIO(media_bytes)
        bytes_io.seek(0)
        bytes_io.name = filename
        return bytes_io, filename
    except Exception as e:
        logger.error(f"[DOWNLOAD] Ошибка id={message.id}: {e}")
        return None, None


async def send_media_safely(user_client, bot_client, msg, files_data, full_caption):
    try:
        media_type = get_media_type(msg)
        prepared_files = []
        for f_io, fname in files_data:
            if not hasattr(f_io, 'name') or not f_io.name:
                f_io.name = fname
            prepared_files.append(f_io)

        total_size = sum(f_io.getbuffer().nbytes for f_io in prepared_files)
        client = user_client if total_size > MAX_BOT_SIZE else bot_client
        client_name = "user" if total_size > MAX_BOT_SIZE else "bot"

        if len(prepared_files) == 1:
            await client.send_file(
                TARGET_CHAT_ID, prepared_files[0],
                caption=full_caption if full_caption else None,
                force_document=(media_type == "document")
            )
        else:
            await client.send_file(
                TARGET_CHAT_ID, prepared_files,
                caption=full_caption if full_caption else None
            )
        logger.info(f"[SEND] Успешно отправлено id={msg.id} через {client_name}")
        return True
    except FloodWaitError as e:
        logger.warning(f"Flood wait: {e.seconds} сек")
        await asyncio.sleep(e.seconds)
        return False
    except Exception as e:
        logger.error(f"[SEND] Ошибка отправки id={msg.id}: {e}")
        return False


async def send_media_via_media_ref(user_client, msg, full_caption):
    try:
        await user_client.send_file(TARGET_CHAT_ID, msg.media, caption=full_caption if full_caption else None)
        return True
    except Exception as e:
        logger.error(f"[MEDIA_REF] Ошибка id={msg.id}: {e}")
        return False


async def forward_latest_posts(user_client, bot_client):
    while True:
        for channel in CHANNELS:
            try:
                entity = await user_client.get_entity(channel)
                await asyncio.sleep(1)

                last_id = last_post_ids.get(channel, 0)
                messages = []
                async for msg in user_client.iter_messages(entity, min_id=last_id):
                    if msg.date < START_DATE:
                        break
                    messages.append(msg)

                messages.reverse()

                if not messages:
                    logger.info(f"Нет новых сообщений в канале {channel} (last_id={last_id})")
                    continue

                logger.info(f"Найдено {len(messages)} новых сообщений.")
                processed_groups = set()

                for msg in messages:
                    if msg.id <= last_post_ids.get(channel, 0):
                        continue
                    if msg.grouped_id and msg.grouped_id in processed_groups:
                        continue

                    caption = (msg.message or "").strip()
                    source = f"\n\nИсточник: https://t.me/c/{str(channel).replace('-100', '')}/{msg.id}"
                    full_caption = truncate_text(caption, source, 1024)

                    success = False
                    max_id_in_album = msg.id

                    if msg.media:
                        try:
                            if msg.grouped_id:
                                album = [msg]
                                async for m in user_client.iter_messages(
                                    entity, min_id=msg.id - 20, max_id=msg.id + 20
                                ):
                                    if m.grouped_id == msg.grouped_id and m.id != msg.id:
                                        album.append(m)
                                album.sort(key=lambda x: x.id)
                                processed_groups.add(msg.grouped_id)
                                max_id_in_album = max(m.id for m in album)
                            else:
                                album = [msg]

                            media_size = msg.document.size if (msg.document and msg.document.size) else 0
                            
                            if media_size > MAX_BOT_SIZE and len(album) == 1:
                                if await send_media_via_media_ref(user_client, msg, full_caption):
                                    success = True
                            else:
                                files_data = []
                                for m in album:
                                    bio, fname = await download_media_bytes(user_client, m)
                                    if bio:
                                        files_data.append((bio, fname))

                                if files_data:
                                    if await send_media_safely(user_client, bot_client, msg, files_data, full_caption):
                                        success = True
                                
                                for f_io, _ in files_data:
                                    try: f_io.close()
                                    except: pass

                        except Exception as e:
                            logger.error(f"[MAIN] Ошибка медиа id={msg.id}: {e}")

                    elif full_caption:
                        try:
                            await bot_client.send_message(TARGET_CHAT_ID, full_caption)
                            success = True
                        except FloodWaitError as e:
                            await asyncio.sleep(e.seconds)

                    if success:
                        last_post_ids[channel] = max_id_in_album
                        save_last_ids(last_post_ids)
                    else:
                        logger.warning(f"[SKIP] Пост id={msg.id} не обработан.")

            except Exception as e:
                logger.error(f"Ошибка при обработке канала {channel}: {e}")

        logger.info("Цикл завершён, ожидание 300 секунд")
        await asyncio.sleep(300)


# ===== 7. ГЛАВНАЯ ФУНКЦИЯ =====
async def main():
    logger.info("Поиск рабочего прокси...")
    
    # 1. Ищем прокси
    working_proxy = await find_working_proxy()
    if not working_proxy:
        logger.error("Не удалось найти рабочий прокси. Завершение работы.")
        return

    # 2. Создаем клиенты с найденным прокси
    logger.info(f"Инициализация клиентов с прокси {working_proxy[1]}:{working_proxy[2]}")
    
    user_client = TelegramClient(
        "YOUR_SESSION_NAME", api_id=API_ID, api_hash=API_HASH,
        proxy=working_proxy, connection_retries=3
    )
    bot_client = TelegramClient(
        "bot_session", api_id=API_ID, api_hash=API_HASH,
        proxy=working_proxy, connection_retries=3
    )

    try:
        await user_client.start()
        logger.info("user_client успешно авторизован")
        await bot_client.start(bot_token=BOT_TOKEN)
        logger.info("bot_client успешно авторизован")
        
        await forward_latest_posts(user_client, bot_client)
    except KeyboardInterrupt:
        logger.info("Парсер остановлен пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}", exc_info=True)
    finally:
        await user_client.disconnect()
        await bot_client.disconnect()

if __name__ == "__main__":
    asyncio.run(main())