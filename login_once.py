from telethon import TelegramClient
from config import API_ID, API_HASH, CLIENT_SESSION_NAME

client = TelegramClient(CLIENT_SESSION_NAME, API_ID, API_HASH)

async def main():
    # 觸發登入流程
    await client.get_me()

with client:
    client.loop.run_until_complete(main())