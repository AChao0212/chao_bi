"""
Trade State Storage Module.

Provides persistent storage for tracked trades using JSON file.
"""

import os
import json
from datetime import datetime
from config import STATE_FILE_PATH
from logger import ModuleLogger

# Initialize logger
log = ModuleLogger("state")

# =============================================================================
# GLOBAL STATE
# =============================================================================

# Key: str(entry_order_id) -> Value: trade record dict
_tracked_trades: dict = {}


# =============================================================================
# STATE PERSISTENCE
# =============================================================================

def load_state() -> None:
    """
    Load tracked trades from state file.

    Creates empty state if file doesn't exist or is invalid.
    """
    global _tracked_trades

    if not os.path.exists(STATE_FILE_PATH):
        log.info("State file not found, starting fresh")
        _tracked_trades.clear()
        return

    try:
        with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)

        if isinstance(data, dict):
            _tracked_trades.clear()
            _tracked_trades.update({str(k): v for k, v in data.items()})
            log.info(f"Loaded {len(_tracked_trades)} trades from state")
        else:
            _tracked_trades.clear()
            log.warning("Invalid state format, starting fresh")

    except Exception as e:
        log.error(f"Failed to load state: {e}")
        _tracked_trades.clear()


def save_state() -> None:
    """Save tracked trades to state file."""
    try:
        with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(_tracked_trades, f, ensure_ascii=False, indent=2)
    except Exception as e:
        log.error(f"Failed to save state: {e}")


# =============================================================================
# TRADE REGISTRATION
# =============================================================================

def register_entry_trade(
    symbol: str,
    position_side: str,
    order_type: str,
    entry_price: str,
    quantity: str,
    leverage: int,
    stop_loss: str,
    take_profit: str,
    entry_order_id: int,
    channel_title: str,
    raw_signal: str
) -> None:
    """
    Register a new entry trade.

    Args:
        symbol: Trading pair
        position_side: 'LONG' or 'SHORT'
        order_type: 'LIMIT' or 'MARKET'
        entry_price: Entry price as string
        quantity: Trade quantity as string
        leverage: Leverage used
        stop_loss: Stop-loss price as string
        take_profit: Take-profit price as string
        entry_order_id: Binance order ID
        channel_title: Signal source channel
        raw_signal: Original signal text
    """
    if not entry_order_id:
        return

    key = str(entry_order_id)
    now_iso = datetime.utcnow().isoformat()

    _tracked_trades[key] = {
        "symbol": symbol,
        "position_side": position_side,
        "order_type": order_type,
        "entry_price": entry_price,
        "quantity": quantity,
        "leverage": leverage,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "entry_order_id": entry_order_id,
        "sl_order_id": None,
        "tp_order_id": None,
        "created_at": now_iso,
        "updated_at": now_iso,
        "channel_title": channel_title,
        "raw_signal": raw_signal,
    }

    save_state()
    log.info(f"Registered trade {entry_order_id}")


def update_exits_for_trade(
    entry_order_id: int,
    sl_order_id: int,
    tp_order_id: int
) -> None:
    """
    Update SL/TP order IDs for a tracked trade.

    Args:
        entry_order_id: Entry order ID
        sl_order_id: Stop-loss order ID
        tp_order_id: Take-profit order ID
    """
    key = str(entry_order_id)

    if key not in _tracked_trades:
        return

    if sl_order_id is not None:
        _tracked_trades[key]["sl_order_id"] = sl_order_id
    if tp_order_id is not None:
        _tracked_trades[key]["tp_order_id"] = tp_order_id

    _tracked_trades[key]["updated_at"] = datetime.utcnow().isoformat()

    save_state()
    log.info(f"Updated SL/TP for trade {entry_order_id}")


def clear_closed_trade(entry_order_id) -> None:
    """
    Remove a closed trade from state.

    Args:
        entry_order_id: Entry order ID to remove
    """
    key = str(entry_order_id)

    if key in _tracked_trades:
        _tracked_trades.pop(key, None)
        save_state()
        log.info(f"Cleared trade {entry_order_id}")


def iter_tracked_trades():
    """
    Iterate over tracked trades safely.

    Returns:
        List of (key, record) tuples
    """
    return list(_tracked_trades.items())
