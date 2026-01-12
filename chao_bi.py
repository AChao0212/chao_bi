"""
Chao Bi - Telegram Signal Trading Bot.

This module provides the main entry point for the trading bot:
- Monitors Telegram channels for trading signals
- Parses signals using LLM
- Executes trades on Binance Futures
"""

import re
import asyncio
from decimal import Decimal, ROUND_DOWN, ROUND_UP

# =============================================================================
# IMPORTS
# =============================================================================

from config import (
    MAX_INITIAL_MARGIN_PCT,
    USE_PY_RISK_MANAGER,
    AUTO_CANCEL_SECONDS,
    ORDER_MONITOR_INTERVAL,
    RECONCILE_INTERVAL,
)
from state_store import register_entry_trade, load_state
from llm import parse_signal_with_llm
from telegram import client, notify_user
from logger import ModuleLogger
import binance_api

# Initialize module logger
log = ModuleLogger("chao_bi")

# =============================================================================
# SYMBOL ALIASES
# =============================================================================

ALIAS_MAP = {
    r"(大餅|比特|比特幣)": "BTC",
    r"(姨太|以太|二餅)": "ETH",
    r"(狗狗|doge)": "DOGE",
    r"(sol|索爾)": "SOL",
    r"(xrp|瑞波)": "XRP",
    r"(sui|水)": "SUI",
}


def normalize_symbol(raw_symbol: str) -> str:
    """
    Normalize symbol name using alias map.

    Args:
        raw_symbol: Raw symbol from signal

    Returns:
        Normalized symbol (e.g., 'BTCUSDT')
    """
    if not raw_symbol:
        return ""

    s = raw_symbol.strip().upper()

    # Apply alias mapping
    for pattern, replacement in ALIAS_MAP.items():
        if re.search(pattern, s, re.IGNORECASE):
            s = replacement
            break

    # Ensure USDT suffix
    if not s.endswith("USDT"):
        s = s + "USDT"

    return s


# =============================================================================
# ORDER PARAMETER CALCULATION
# =============================================================================

def compute_order_parameters(
    symbol: str,
    entry_price_str: str,
    action: str,
    sl_str: str,
    tp_str: str,
    leverage: int
) -> tuple[str, str, str, str, Decimal]:
    """
    Compute and validate order parameters.

    This function:
    1. Fetches exchange rules (price precision, lot size, etc.)
    2. Calculates quantity based on risk and margin limits
    3. Formats all values according to exchange requirements

    Args:
        symbol: Trading pair
        entry_price_str: Entry price (or None for market orders)
        action: 'BUY' or 'SELL'
        sl_str: Stop-loss price
        tp_str: Take-profit price
        leverage: Leverage to use

    Returns:
        Tuple of (quantity, price, stop_loss, take_profit, reference_price)

    Raises:
        RuntimeError: If parameters cannot be computed
    """
    # Get exchange rules
    resp = binance_api.binance_client.rest_api.exchange_information()
    info = binance_api._to_dict(resp.data())
    symbols_list = info.get("symbols") or []
    symbol_info = None
    for s in symbols_list:
        s_dict = binance_api._to_dict(s) if not isinstance(s, dict) else s
        if s_dict.get("symbol") == symbol:
            symbol_info = s_dict
            break

    if not symbol_info:
        raise RuntimeError(f"Symbol {symbol} not found in exchange info")

    # Extract filters
    filters = symbol_info.get("filters") or []
    price_filter = None
    lot_filter = None
    min_notional_filter = None

    for f in filters:
        f_dict = binance_api._to_dict(f) if not isinstance(f, dict) else f
        filter_type = f_dict.get("filterType") or f_dict.get("filter_type")
        if filter_type == "PRICE_FILTER":
            price_filter = f_dict
        elif filter_type == "LOT_SIZE":
            lot_filter = f_dict
        elif filter_type == "MIN_NOTIONAL":
            min_notional_filter = f_dict

    if not price_filter or not lot_filter:
        raise RuntimeError("Missing PRICE_FILTER or LOT_SIZE filter")

    price_precision = price_filter.get("tickSize") or price_filter.get("tick_size") or "0.00000001"
    quantity_precision = lot_filter.get("stepSize") or lot_filter.get("step_size") or "0.001"
    min_qty = Decimal(lot_filter.get("minQty") or lot_filter.get("min_qty") or "0")

    # Get min notional from filter, default to 100 USDT (Binance futures minimum)
    min_notional = Decimal("100")  # Default minimum for USDS-M futures
    if min_notional_filter:
        filter_notional = Decimal(min_notional_filter.get("notional") or min_notional_filter.get("minNotional") or "0")
        if filter_notional > 0:
            min_notional = max(filter_notional, Decimal("100"))  # At least 100 USDT

    # Get reference price
    if entry_price_str:
        ref_price = Decimal(str(entry_price_str))
    else:
        market_price = binance_api.get_market_price(symbol)
        if not market_price:
            raise RuntimeError(f"Cannot get market price for {symbol}")
        ref_price = Decimal(str(market_price))

    # Calculate SL/TP
    sl_dec = Decimal(str(sl_str)) if sl_str else None
    tp_dec = Decimal(str(tp_str)) if tp_str else None

    if sl_dec is None or tp_dec is None:
        raise RuntimeError("Missing SL or TP")

    # Refresh available balance before calculation
    available_balance = binance_api.refresh_available_balance()
    if available_balance <= 0:
        raise RuntimeError("No available balance")

    # Calculate quantity based on risk
    sl_distance = abs(ref_price - sl_dec)
    if sl_distance == 0:
        raise RuntimeError("SL distance is zero")

    risk_amount = Decimal(str(available_balance)) * Decimal("0.01")
    quantity = (risk_amount * Decimal(str(leverage))) / sl_distance

    # Calculate minimum quantity for notional requirement (100 USDT min for futures)
    min_qty_for_notional = (min_notional / ref_price).quantize(
        Decimal(quantity_precision),
        rounding=ROUND_UP
    )

    # Use the higher of LOT_SIZE min_qty and notional min_qty
    effective_min_qty = max(min_qty, min_qty_for_notional)

    # Ensure minimum notional
    if quantity < effective_min_qty:
        quantity = effective_min_qty

    # Cap by margin limit
    max_margin = Decimal(str(available_balance)) * Decimal(str(MAX_INITIAL_MARGIN_PCT))
    original_qty = quantity
    quantity = binance_api.cap_quantity_by_margin(
        ref_price,
        Decimal(str(leverage)),
        quantity,
        max_margin,
        Decimal(quantity_precision),
        effective_min_qty  # Use effective min qty (includes notional requirement)
    )

    if quantity <= 0:
        # Calculate minimum required balance for this trade
        min_notional_value = effective_min_qty * ref_price
        min_margin_required = min_notional_value / Decimal(str(leverage))
        min_balance_required = min_margin_required / Decimal(str(MAX_INITIAL_MARGIN_PCT))
        raise RuntimeError(
            f"Insufficient balance. Need ~{min_balance_required:.2f} USDT for {symbol} "
            f"(min_notional={min_notional}, min_qty={effective_min_qty}, balance={available_balance:.2f})"
        )

    # Format values
    f_qty = binance_api.format_value(quantity, quantity_precision)
    f_price = binance_api.format_value(ref_price, price_precision) if entry_price_str else None
    f_sl = binance_api.format_value(sl_dec, price_precision)
    f_tp = binance_api.format_value(tp_dec, price_precision)

    return (f_qty, f_price, f_sl, f_tp, ref_price)


# =============================================================================
# TRADE EXECUTION
# =============================================================================

def execute_trade(trade_params: dict, event_loop=None) -> None:
    """
    Execute a trade based on parsed signal parameters.

    Args:
        trade_params: Dict containing:
            - symbol: Trading pair
            - action: 'BUY' or 'SELL'
            - entry_price: Entry price (or None for market)
            - stop_loss: Stop-loss price
            - take_profit: Take-profit price
            - leverage: Leverage (optional)
            - channel_title: Source channel
            - raw_signal: Original signal text
        event_loop: Event loop for async operations
    """
    log.info("=" * 30)
    log.info("Processing trade signal")

    symbol = trade_params.get("symbol")
    action = trade_params.get("action")
    entry_price = trade_params.get("entry_price")
    sl = trade_params.get("stop_loss")
    tp = trade_params.get("take_profit")
    leverage_hint = trade_params.get("leverage")
    channel_title = trade_params.get("channel_title", "Unknown")
    raw_signal = trade_params.get("raw_signal", "")

    # Validate symbol
    if not binance_api.is_valid_symbol(symbol):
        log.error(f"Invalid symbol: {symbol}")
        notify_user(f"Trade rejected: Invalid symbol {symbol}", loop=event_loop)
        return

    # Check for duplicate position or pending order
    position_side = "LONG" if action == "BUY" else "SHORT"
    has_duplicate, reason = binance_api.has_existing_position_or_order(symbol, position_side)
    if has_duplicate:
        log.warning(f"Duplicate order prevented: {reason}")
        notify_user(f"Trade skipped: {reason}", loop=event_loop)
        log.info("=" * 30)
        return

    log.info(f"Symbol: {symbol}")
    log.info(f"Action: {action}")
    log.info(f"Entry: {entry_price or 'MARKET'}")
    log.info(f"SL: {sl}, TP: {tp}")

    # Set leverage
    leverage = binance_api.set_leverage(symbol, int(leverage_hint) if leverage_hint else None)
    if leverage == 0:
        log.error("Failed to set leverage")
        notify_user(f"Trade rejected: Cannot set leverage for {symbol}", loop=event_loop)
        return

    log.info(f"Leverage: {leverage}x")

    # Get reference price for calculations
    if entry_price:
        ref_price = Decimal(str(entry_price))
    else:
        market_price = binance_api.get_market_price(symbol)
        if not market_price:
            log.error("Cannot get market price")
            notify_user(f"Trade rejected: Cannot get price for {symbol}", loop=event_loop)
            return
        ref_price = Decimal(str(market_price))
        log.info(f"Using market price: {ref_price}")

    # Calculate SL/TP if using Python risk manager
    if USE_PY_RISK_MANAGER or sl is None or tp is None:
        log.info("Computing SL/TP using risk manager...")
        final_sl, final_tp, warnings = binance_api.select_sl_tp_with_preference(
            symbol, action, ref_price, sl, tp
        )
        for w in warnings:
            log.warning(w)
    else:
        final_sl = Decimal(str(sl))
        final_tp = Decimal(str(tp))

    log.info(f"Final SL: {final_sl}, TP: {final_tp}")

    # Compute order parameters
    try:
        f_qty, f_price, f_sl, f_tp, _ = compute_order_parameters(
            symbol,
            str(entry_price) if entry_price else None,
            action,
            str(final_sl),
            str(final_tp),
            leverage
        )
    except Exception as e:
        log.error(f"Parameter calculation failed: {e}")
        notify_user(f"Trade rejected: {symbol} - {e}", loop=event_loop)
        return

    log.info(f"Quantity: {f_qty}")

    # Build order
    position_side = "LONG" if action == "BUY" else "SHORT"
    order_type = "LIMIT" if f_price else "MARKET"

    order_params = {
        "symbol": symbol,
        "side": action,
        "position_side": position_side,
        "type": order_type,
        "quantity": float(f_qty),
        "new_order_resp_type": "RESULT",
    }

    if order_type == "LIMIT":
        order_params["price"] = float(f_price)
        order_params["time_in_force"] = "GTC"

    # Place order
    try:
        log.info("Placing entry order...")
        resp = binance_api.binance_client.rest_api.new_order(**order_params)
        entry_resp = binance_api._to_dict(resp.data())
        order_id = entry_resp.get("orderId") or entry_resp.get("order_id")
        status = entry_resp.get("status")
        log.info(f"Order placed. Status: {status}, ID: {order_id}")

        binance_api._monitoring_orders.add((symbol, order_id))

        # Register trade in state
        try:
            register_entry_trade(
                symbol=symbol,
                position_side=position_side,
                order_type=order_type,
                entry_price=(f_price or str(ref_price)),
                quantity=f_qty,
                leverage=leverage,
                stop_loss=f_sl,
                take_profit=f_tp,
                entry_order_id=order_id,
                channel_title=channel_title,
                raw_signal=raw_signal,
            )
        except Exception as e:
            log.error(f"Failed to register trade state: {e}")

        # Notify user
        notify_user(
            f"Order placed\n"
            f"Symbol: {symbol} ({action})\n"
            f"Type: {order_type} @ {f_price or 'MARKET'}\n"
            f"Quantity: {f_qty} (x{leverage})\n"
            f"OrderID: {order_id}",
            loop=event_loop
        )

    except Exception as e:
        log.error(f"Order placement failed: {e}")
        notify_user(f"Order failed: {symbol} - {e}", loop=event_loop)
        log.info("=" * 30)
        return

    # Handle post-order actions
    if order_type != "MARKET":
        # Start monitoring for LIMIT orders
        try:
            if event_loop and event_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    binance_api.monitor_entry_order(
                        symbol, order_id, position_side, f_sl, f_tp,
                        AUTO_CANCEL_SECONDS, ORDER_MONITOR_INTERVAL
                    ),
                    event_loop
                )
                log.info(f"Started order monitor for {order_id}")
            else:
                log.warning("Cannot start monitor: event loop not available")
        except Exception as e:
            log.error(f"Failed to start monitor: {e}")
    else:
        # Attach SL/TP immediately for MARKET orders
        sl_id, tp_id = binance_api.attach_exit_orders(
            symbol, position_side, f_sl, f_tp, entry_order_id=order_id
        )
        notify_user(
            f"SL/TP attached\nSL: {f_sl} (ID: {sl_id})\nTP: {f_tp} (ID: {tp_id})",
            loop=event_loop
        )

    log.info("=" * 30)


# =============================================================================
# SIGNAL PROCESSING
# =============================================================================

async def process_signal(message_text: str, channel_title: str, event_loop) -> None:
    """
    Process a potential trading signal message.

    Args:
        message_text: The message text to process
        channel_title: Source channel/group name
        event_loop: Event loop for async operations
    """
    if not message_text or len(message_text) < 5:
        return

    log.info(f"Processing message from {channel_title}")

    # Parse with LLM
    try:
        parsed = await event_loop.run_in_executor(None, parse_signal_with_llm, message_text)
    except Exception as e:
        log.error(f"LLM parsing failed: {e}")
        return

    if not parsed:
        log.info("LLM returned no result")
        return

    action = (parsed.get("action") or "").upper()
    if action not in ("BUY", "SELL"):
        log.info(f"Not a trade signal (action={action})")
        return

    # Normalize symbol
    raw_symbol = parsed.get("symbol") or ""
    symbol = normalize_symbol(raw_symbol)

    if not symbol or not binance_api.is_valid_symbol(symbol):
        log.warning(f"Invalid or unknown symbol: {raw_symbol} -> {symbol}")
        return

    log.info(f"Valid signal detected: {symbol} {action}")

    # Build trade parameters
    trade_params = {
        "symbol": symbol,
        "action": action,
        "entry_price": parsed.get("entry_price"),
        "stop_loss": parsed.get("stop_loss"),
        "take_profit": parsed.get("take_profit"),
        "leverage": parsed.get("leverage"),
        "channel_title": channel_title,
        "raw_signal": message_text,
    }

    # Execute trade
    await event_loop.run_in_executor(None, execute_trade, trade_params, event_loop)


# =============================================================================
# TELEGRAM EVENT HANDLERS
# =============================================================================

async def handle_message(event) -> None:
    """Handle incoming Telegram messages."""
    try:
        message = event.message
        if not message or not message.text:
            return

        # Skip outgoing messages (sent by us/bot)
        if message.out:
            return

        # Skip messages from bots
        sender = await message.get_sender()
        if sender and getattr(sender, "bot", False):
            return

        chat = await event.get_chat()
        channel_title = getattr(chat, "title", None) or getattr(chat, "username", None)
        message_text = message.text.strip()

        # Skip messages that look like bot notifications
        bot_patterns = [
            "Order placed", "Order filled", "Order cancelled", "Order failed",
            "Trade rejected", "SL/TP attached", "Position closed",
            "Cleaned orphan", "Cancelled stale", "Reconciliation complete",
        ]
        if any(message_text.startswith(p) for p in bot_patterns):
            return

        loop = asyncio.get_running_loop()
        await process_signal(message_text, channel_title, loop)

    except Exception as e:
        log.error(f"Message handling failed: {e}")


# =============================================================================
# BACKGROUND TASKS
# =============================================================================

async def periodic_reconcile_task(loop: asyncio.AbstractEventLoop, interval_sec: int = RECONCILE_INTERVAL) -> None:
    """
    Periodically run reconciliation and state cleanup.

    Args:
        loop: Event loop
        interval_sec: Interval between runs (default from config.RECONCILE_INTERVAL)
    """
    while True:
        try:
            await loop.run_in_executor(None, binance_api.reconcile_orders, loop)
        except Exception as e:
            log.error(f"Reconciliation failed: {e}")

        try:
            log.info("Running state cleanup...")
            await loop.run_in_executor(None, binance_api.resume_tracked_trades, loop)
        except Exception as e:
            log.error(f"State cleanup failed: {e}")

        await asyncio.sleep(interval_sec)


# =============================================================================
# MAIN ENTRY POINT
# =============================================================================

async def main() -> None:
    """Main entry point - start Telegram client and background tasks."""
    log.info("Starting Chao Bi trading bot...")

    # Connect Telegram client
    await client.start()
    log.info("Telegram client connected")

    loop = asyncio.get_running_loop()

    # Set up Telegram integration in binance_api
    binance_api.set_telegram_client(client, notify_user)

    # Load saved state
    try:
        load_state()
    except Exception as e:
        log.error(f"Failed to load state: {e}")

    # Register message handler
    try:
        from telethon import events
        client.add_event_handler(handle_message, events.NewMessage())
        log.info("Message handler registered")
    except Exception as e:
        log.error(f"Failed to register handler: {e}")

    # Start background tasks
    asyncio.create_task(periodic_reconcile_task(loop))
    asyncio.create_task(binance_api.daily_pnl_notifier("Asia/Taipei", 0, 0))
    asyncio.create_task(binance_api.monitor_position_closes(poll_interval=60))

    log.info("Listening for messages...")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
