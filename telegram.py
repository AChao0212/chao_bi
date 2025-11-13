import requests
from config import (
    BOT_TOKEN, BOT_CHAT_ID,
    API_ID, API_HASH,
    CLIENT_SESSION_NAME
)
# --- 導入 Telethon (v32) ---
try:
    from telethon import TelegramClient
    from telethon.tl.functions.messages import ImportChatInviteRequest
except ImportError as e:
    print(f"[error] 致命錯誤：找不到 'telethon' 模組！")
    print(f"錯誤詳情: {e}")
    exit()

# --- 5. 📞 Telethon 客戶端 (v32 工作流) ---
client = None
if not API_ID or not API_HASH:
    print("[error] 找不到 'telegram.txt' 或金鑰不完整。")
else:
    try:
        client = TelegramClient(CLIENT_SESSION_NAME, API_ID, API_HASH)
    except Exception as e:
        print(f"[error] Telethon 錯誤: {e}")
        client = None

def notify_via_bot_api(text: str) -> bool:
    """若提供 BOT_TOKEN/BOT_CHAT_ID，透過 Telegram Bot API 送訊息（會觸發推播）。"""
    if not BOT_TOKEN or not BOT_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": BOT_CHAT_ID,
            "text": text,
            "disable_notification": False,  # 確保會推播
            "parse_mode": "HTML"
        }
        r = requests.post(url, data=payload, timeout=10)
        if r.status_code == 200 and r.json().get("ok"):
            return True
        else:
            print(f"⚠️ Bot API 通知失敗：{r.status_code} {r.text}")
            return False
    except Exception as e:
        print(f"⚠️ Bot API 通知例外：{e}")
        return False

def notify_user(text: str, loop=None):
    """
    先嘗試用 Bot API（可推播），失敗才退回 Telethon（同帳號訊息可能不推播）。
    """
    try:
        # 1) 優先走 Bot 推播
        if notify_via_bot_api(text):
            return
    except Exception as e:
        print(f"⚠️ 通知排程失敗：{e}")