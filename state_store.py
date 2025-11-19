# state_store.py
import os
import json
from datetime import datetime
from config import STATE_FILE_PATH

# key: str(entry_order_id) → value: dict
_tracked_trades = {}

def load_state():
    global _tracked_trades
    if not os.path.exists(STATE_FILE_PATH):
        _tracked_trades.clear()
        return
    try:
        with open(STATE_FILE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
            if isinstance(data, dict):
                _tracked_trades.clear()
                _tracked_trades.update({str(k): v for k, v in data.items()})
            else:
                _tracked_trades.clear()
    except Exception as e:
        print(f"⚠️ 載入狀態檔失敗，將從空白開始：{e}")
        _tracked_trades.clear()

def save_state():
    """將目前追蹤中的交易寫回 JSON，供重啟後恢復。"""
    try:
        with open(STATE_FILE_PATH, "w", encoding="utf-8") as f:
            json.dump(_tracked_trades, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ 寫入狀態檔失敗：{e}")

def register_entry_trade(symbol, position_side, order_type, entry_price, quantity,
                         leverage, stop_loss, take_profit, entry_order_id,
                         channel_title, raw_signal):
    """
    註冊一筆新的開倉交易。
    建議傳進來的 entry_price / stop_loss / take_profit / quantity / leverage 都是字串。
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
    print(f"📝 已記錄開倉單 {entry_order_id} 於狀態檔。")

def update_exits_for_trade(entry_order_id, sl_order_id, tp_order_id):
    """在 SL/TP 掛單成功後更新對應的出場單 ID。"""
    key = str(entry_order_id)
    if key not in _tracked_trades:
        return
    if sl_order_id is not None:
        _tracked_trades[key]["sl_order_id"] = sl_order_id
    if tp_order_id is not None:
        _tracked_trades[key]["tp_order_id"] = tp_order_id
    _tracked_trades[key]["updated_at"] = datetime.utcnow().isoformat()
    save_state()
    print(f"📝 已更新開倉單 {entry_order_id} 的 SL/TP ID。")

def clear_closed_trade(entry_order_id):
    """當開倉單確定不再需要追蹤（撤單/完成/錯誤）時，從狀態檔移除。"""
    key = str(entry_order_id)
    if key in _tracked_trades:
        _tracked_trades.pop(key, None)
        save_state()
        print(f"🧹 已自狀態檔移除開倉單 {entry_order_id}。")

def iter_tracked_trades():
    """提供一個安全的 iterator 給外面使用。"""
    return list(_tracked_trades.items())