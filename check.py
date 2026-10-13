import os
import io
import sys
import asyncio
from dotenv import load_dotenv
from telethon import TelegramClient
import TelethonFakeTLS

# Загружаем переменные из .env
load_dotenv()

API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")

# Прокси (тот же, что в main.py)
proxy = (
    "max.kimt.click",
    443,
    "1b153cf06dbd43c6085c359a6702eb936d61782e6b696d742e636c69636b"
)

# Создаём клиент (имя сессии должно совпадать с main.py!)
user_client = TelegramClient(
    "YOUR_SESSION_NAME",
    api_id=API_ID,
    api_hash=API_HASH,
    connection=TelethonFakeTLS.ConnectionTcpMTProxyFakeTLS,
    proxy=proxy
)


async def check():
    async with user_client:
        try:
            entity = await user_client.get_entity(-1001461849007)
            print(f"✅ Канал: {entity.title}")
            print(f"   ID: {entity.id}")
        except Exception as e:
            print(f"❌ Не удалось найти канал: {e}")
            return

        print("\n📩 Последние 5 сообщений:")
        count = 0
        async for msg in user_client.iter_messages(entity, limit=5):
            count += 1
            print(f"   ID: {msg.id} | дата: {msg.date} | текст: {(msg.message or '')[:50]}")

        if count == 0:
            print("\n⚠️ Сообщений не найдено!")
        else:
            print(f"\n✅ Всего найдено сообщений: {count}")


if __name__ == "__main__":
    asyncio.run(check())