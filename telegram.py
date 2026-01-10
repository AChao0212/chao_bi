"""
Telegram Integration Module.

Provides Telegram client setup and notification functions.
"""

import requests
from config import (
    BOT_TOKEN,
    BOT_CHAT_ID,
    API_ID,
    API_HASH,
    CLIENT_SESSION_NAME,
)
from logger import ModuleLogger

# Initialize logger
log = ModuleLogger("telegram")

# =============================================================================
# TELEGRAM CLIENT SETUP
# =============================================================================

try:
    from telethon import TelegramClient
    from telethon.tl.functions.messages import ImportChatInviteRequest
except ImportError as e:
    log.error(f"Telethon module not found: {e}")
    exit(1)

client = None
try:
    client = TelegramClient(CLIENT_SESSION_NAME, API_ID, API_HASH)
except Exception as e:
    log.error(f"Failed to create Telegram client: {e}")
    client = None


# =============================================================================
# NOTIFICATION FUNCTIONS
# =============================================================================

def notify_via_bot_api(text: str) -> bool:
    """
    Send notification via Telegram Bot API.

    This method is preferred as it triggers push notifications.

    Args:
        text: Message text to send

    Returns:
        True if successful, False otherwise
    """
    if not BOT_TOKEN or not BOT_CHAT_ID:
        return False

    try:
        url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": BOT_CHAT_ID,
            "text": text,
            "disable_notification": False,
            "parse_mode": "HTML",
        }
        response = requests.post(url, data=payload, timeout=10)

        if response.status_code == 200 and response.json().get("ok"):
            return True
        else:
            log.error(f"Bot API failed: {response.status_code} {response.text}")
            return False

    except Exception as e:
        log.error(f"Bot API exception: {e}")
        return False


def notify_user(text: str, loop=None) -> None:
    """
    Send notification to user.

    Tries Bot API first (supports push notifications),
    falls back to Telethon client if Bot API fails.

    Args:
        text: Message text to send
        loop: Event loop for async operations (optional)
    """
    try:
        if notify_via_bot_api(text):
            return
    except Exception as e:
        log.error(f"Notification failed: {e}")
