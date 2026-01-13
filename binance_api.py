"""
Binance USDS-M Futures API Module.

This module provides a complete interface for interacting with Binance USDS-M Futures,
including order management, position tracking, risk management, and trade logging.

Sections:
    1. Imports and Configuration
    2. Global State
    3. Client Initialization
    4. Market Data Functions
    5. Account & Position Functions
    6. Order Management Functions
    7. Risk Management (ATR, SL/TP Calculation)
    8. Trade State Management
    9. Monitoring & Reconciliation
    10. Daily Summary & Notifications
"""

import time
import json
import asyncio
from decimal import Decimal, ROUND_DOWN
from datetime import datetime, timedelta
from typing import Optional

# =============================================================================
# 1. IMPORTS AND CONFIGURATION
# =============================================================================

from config import (
    DEFAULT_LEVERAGE,
    LEVERAGE_OVERRIDES,
    BINANCE_API_KEY,
    BINANCE_API_SECRET,
    REAL_FUTURES_BASE_URL,
    RR_DEFAULT,
    RR_MAX,
    MIN_STOP_DISTANCE_PCT,
    ATR_K,
    ATR_PERIOD,
    RECONCILE_VERBOSE,
    AUTO_CANCEL_SECONDS,
    ORDER_MONITOR_INTERVAL,
)
from trade_logger import log_trade
from state_store import (
    _tracked_trades,
    update_exits_for_trade,
    clear_closed_trade,
)
from logger import ModuleLogger, setup_telegram_notifier

# Binance SDK imports
from binance_common.configuration import ConfigurationRestAPI
from binance_common.constants import DERIVATIVES_TRADING_USDS_FUTURES_REST_API_PROD_URL
from binance_sdk_derivatives_trading_usds_futures.derivatives_trading_usds_futures import (
    DerivativesTradingUsdsFutures,
)
from binance_common.errors import ClientError

# Timezone support
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


def _to_dict(obj) -> dict:
    """
    Convert SDK response object to dictionary.

    The new Binance SDK returns typed response objects instead of dicts.
    This helper converts them to dicts for consistent access.

    Handles OneOf wrapper pattern where actual data is in 'actual_instance'.
    """
    if obj is None:
        return {}
    if isinstance(obj, dict):
        # Check for OneOf wrapper pattern: {'actual_instance': ..., 'one_of_schemas': ...}
        if "actual_instance" in obj and obj["actual_instance"] is not None:
            return _to_dict(obj["actual_instance"])
        return obj
    if isinstance(obj, list):
        return {"_list": obj}
    # Try model_dump for pydantic models first
    if hasattr(obj, "model_dump"):
        result = obj.model_dump()
        # Check if model_dump result has actual_instance (OneOf wrapper)
        if isinstance(result, dict) and "actual_instance" in result and result["actual_instance"] is not None:
            return _to_dict(result["actual_instance"])
        return result
    # Check for OneOf wrapper as object attribute
    if hasattr(obj, "actual_instance") and getattr(obj, "actual_instance", None) is not None:
        return _to_dict(obj.actual_instance)
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    # Try to convert object to dict via __dict__
    if hasattr(obj, "__dict__"):
        d = vars(obj)
        # Check for OneOf wrapper in __dict__
        if "actual_instance" in d and d["actual_instance"] is not None:
            return _to_dict(d["actual_instance"])
        return d
    return {}

# =============================================================================
# 2. GLOBAL STATE
# =============================================================================

# Logger instance
log = ModuleLogger("binance")

# Telegram client (set during runtime)
_telegram_client = None
_telegram_notify_func = None

# Binance client and account state
binance_client: Optional[DerivativesTradingUsdsFutures] = None
total_available_margin: float = 0.0

# Symbol info cache for exchange rules
_symbol_info_cache: dict = {}

# Track active order monitors to prevent duplicates
_monitoring_orders: set[tuple[str, int]] = set()


def set_telegram_client(client, notify_func):
    """
    Set the Telegram client and notification function for this module.

    Args:
        client: Telethon client instance
        notify_func: Function to send notifications (signature: notify_func(text, loop=None))
    """
    global _telegram_client, _telegram_notify_func
    _telegram_client = client
    _telegram_notify_func = notify_func
    setup_telegram_notifier(notify_func)


def _notify_user(text: str, loop=None) -> None:
    """Internal helper to send Telegram notifications."""
    if _telegram_notify_func:
        try:
            _telegram_notify_func(text, loop=loop)
        except Exception:
            pass


# =============================================================================
# 3. CLIENT INITIALIZATION
# =============================================================================

def _initialize_client() -> Optional[DerivativesTradingUsdsFutures]:
    """
    Initialize the Binance USDS-M Futures client.

    Returns:
        Configured client instance or None if initialization fails
    """
    global total_available_margin

    try:
        # Configure REST API client
        config = ConfigurationRestAPI(
            api_key=BINANCE_API_KEY,
            api_secret=BINANCE_API_SECRET,
            base_path=REAL_FUTURES_BASE_URL or DERIVATIVES_TRADING_USDS_FUTURES_REST_API_PROD_URL,
        )
        client = DerivativesTradingUsdsFutures(config_rest_api=config)

        # Ensure hedge mode is enabled
        _ensure_hedge_mode(client)

        # Get account balance
        resp = client.rest_api.account_information_v3()
        raw_data = resp.data()

        # Try to access available_balance directly as attribute (new SDK uses snake_case)
        if hasattr(raw_data, "available_balance"):
            total_available_margin = float(raw_data.available_balance or 0)
        elif hasattr(raw_data, "availableBalance"):
            total_available_margin = float(raw_data.availableBalance or 0)
        else:
            account_info = _to_dict(raw_data)
            total_available_margin = float(account_info.get("availableBalance") or account_info.get("available_balance") or 0)

        if total_available_margin <= 0:
            log.error("Total available margin is 0")
            return None

        log.info("Binance connection successful")
        log.info(f"Available balance: {total_available_margin} USDT")

        return client

    except ClientError as e:
        log.error(f"API authentication failed: {e}")
        return None
    except Exception as e:
        log.error(f"Connection failed: {e}")
        return None


def _ensure_hedge_mode(client: DerivativesTradingUsdsFutures) -> None:
    """Ensure the account is in hedge mode (dual position side)."""
    try:
        resp = client.rest_api.get_current_position_mode()
        mode = _to_dict(resp.data())

        # Check for both camelCase and snake_case field names
        dual_side = mode.get("dualSidePosition") or mode.get("dual_side_position")

        if dual_side is False:
            log.info("Switching to hedge mode...")
            client.rest_api.change_position_mode(dual_side_position=True)
            log.info("Successfully switched to hedge mode")
        else:
            log.info("Account is already in hedge mode")

    except ClientError as e:
        if getattr(e, "error_code", None) == -4059:
            log.info("Account is already in hedge mode")
        else:
            raise


# Initialize client on module load
binance_client = _initialize_client()


# =============================================================================
# 4. MARKET DATA FUNCTIONS
# =============================================================================

def get_symbol_info(symbol: str) -> Optional[dict]:
    """
    Get trading rules and filters for a symbol.

    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')

    Returns:
        Symbol info dict or None if not found
    """
    if symbol in _symbol_info_cache:
        return _symbol_info_cache[symbol]

    if binance_client is None:
        return None

    try:
        log.info("Fetching exchange information...")
        resp = binance_client.rest_api.exchange_information()
        info = _to_dict(resp.data())

        # Cache all symbols - handle both dict and list responses
        symbols_list = info.get("symbols") or info.get("_list") or []
        for item in symbols_list:
            item_dict = _to_dict(item)
            if "symbol" in item_dict:
                _symbol_info_cache[item_dict["symbol"]] = item_dict

        return _symbol_info_cache.get(symbol)

    except ClientError as e:
        log.error(f"Failed to get exchange info: {e}")
        return None


def is_valid_symbol(symbol: str) -> bool:
    """Check if a trading pair exists on Binance."""
    return get_symbol_info(symbol) is not None


def get_market_price(symbol: str) -> Optional[str]:
    """
    Get the current market price for a symbol.

    Args:
        symbol: Trading pair (e.g., 'BTCUSDT')

    Returns:
        Price as string or None if failed
    """
    if binance_client is None:
        return None

    # Try symbol_price_ticker_v2 first (v1 is deprecated)
    try:
        resp = binance_client.rest_api.symbol_price_ticker_v2(symbol=symbol)
        raw_data = resp.data()
        ticker = _to_dict(raw_data)
        log.info(f"symbol_price_ticker_v2 response: {ticker}")
        price = ticker.get("price")
        if price:
            return str(price)
    except Exception as e:
        log.warning(f"symbol_price_ticker_v2 failed for {symbol}: {e}")

    # Fallback: try mark_price
    try:
        resp = binance_client.rest_api.mark_price(symbol=symbol)
        raw_data = resp.data()
        mark_data = _to_dict(raw_data)
        # SDK uses snake_case: mark_price
        price = mark_data.get("mark_price") or mark_data.get("markPrice")
        if price:
            return str(price)
    except Exception as e:
        log.warning(f"mark_price failed for {symbol}: {e}")

    # Fallback: try ticker24hr_price_change_statistics
    try:
        resp = binance_client.rest_api.ticker24hr_price_change_statistics(symbol=symbol)
        raw_data = resp.data()
        ticker_data = _to_dict(raw_data)
        # SDK uses snake_case: last_price
        price = ticker_data.get("last_price") or ticker_data.get("lastPrice")
        if price:
            return str(price)
    except Exception as e:
        log.warning(f"ticker24hr_price_change_statistics failed for {symbol}: {e}")

    log.error(f"All price methods failed for {symbol}")
    return None


def get_klines(symbol: str, interval: str = "5m", limit: int = 200) -> list[dict]:
    """
    Get kline/candlestick data for a symbol.

    Args:
        symbol: Trading pair
        interval: Kline interval (e.g., '1m', '5m', '1h')
        limit: Number of klines to fetch (max 1500)

    Returns:
        List of kline dicts with keys: open, high, low, close
    """
    if binance_client is None:
        return []

    try:
        # New SDK uses kline_candlestick_data instead of klines
        resp = binance_client.rest_api.kline_candlestick_data(symbol=symbol, interval=interval, limit=limit)
        raw_data = resp.data()

        # Handle response - could be list directly or wrapped in dict
        if isinstance(raw_data, list):
            raw_klines = raw_data
        else:
            temp = _to_dict(raw_data)
            raw_klines = temp.get("_list") or []

        return [
            {
                "open": Decimal(str(k[1])),
                "high": Decimal(str(k[2])),
                "low": Decimal(str(k[3])),
                "close": Decimal(str(k[4])),
            }
            for k in raw_klines
        ]
    except ClientError as e:
        log.error(f"Failed to get klines for {symbol}: {e}")
        return []
    except AttributeError as e:
        log.error(f"Kline method not found: {e}")
        return []


def get_price_bounds(symbol: str) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """
    Get min/max price bounds for a symbol.

    Returns:
        Tuple of (min_price, max_price), either can be None
    """
    info = get_symbol_info(symbol)
    if not info:
        return (None, None)

    try:
        price_filter = next(
            (f for f in info["filters"] if f["filterType"] == "PRICE_FILTER"),
            None
        )
        if not price_filter:
            return (None, None)

        min_price = Decimal(price_filter.get("minPrice", "0"))
        max_price = Decimal(price_filter.get("maxPrice", "0"))

        return (
            min_price if min_price > 0 else None,
            max_price if max_price > 0 else None,
        )
    except Exception:
        return (None, None)


# =============================================================================
# 5. ACCOUNT & POSITION FUNCTIONS
# =============================================================================

def get_account_info() -> Optional[dict]:
    """Get current account information."""
    if binance_client is None:
        return None

    try:
        resp = binance_client.rest_api.account_information_v3()
        return _to_dict(resp.data())
    except Exception as e:
        log.error(f"Failed to get account info: {e}")
        return None


def refresh_available_balance() -> float:
    """
    Refresh and return the current available balance.

    This should be called before each trade to get the latest balance.

    Returns:
        Available balance in USDT, or 0 if failed
    """
    global total_available_margin

    if binance_client is None:
        return 0.0

    try:
        resp = binance_client.rest_api.account_information_v3()
        raw_data = resp.data()

        # Try to access available_balance directly as attribute
        if hasattr(raw_data, "available_balance"):
            total_available_margin = float(raw_data.available_balance or 0)
        elif hasattr(raw_data, "availableBalance"):
            total_available_margin = float(raw_data.availableBalance or 0)
        else:
            account_info = _to_dict(raw_data)
            total_available_margin = float(
                account_info.get("availableBalance") or
                account_info.get("available_balance") or 0
            )

        log.info(f"Refreshed balance: {total_available_margin:.2f} USDT")
        return total_available_margin

    except Exception as e:
        log.error(f"Failed to refresh balance: {e}")
        return total_available_margin  # Return last known value


def get_position_amount(symbol: str, position_side: str) -> Decimal:
    """
    Get the position amount for a specific symbol and side.

    Args:
        symbol: Trading pair
        position_side: 'LONG' or 'SHORT'

    Returns:
        Position amount as Decimal (0 if not found)
    """
    try:
        account = get_account_info()
        if not account:
            return Decimal("0")

        positions = account.get("positions") or []
        for pos in positions:
            pos_dict = _to_dict(pos) if not isinstance(pos, dict) else pos
            pos_symbol = pos_dict.get("symbol")
            pos_side = (pos_dict.get("positionSide") or pos_dict.get("position_side") or "").upper()
            pos_amt = pos_dict.get("positionAmt") or pos_dict.get("position_amt") or "0"

            if pos_symbol == symbol and pos_side == position_side.upper():
                return Decimal(str(pos_amt))

        return Decimal("0")
    except Exception as e:
        log.error(f"Failed to get position amount: {e}")
        return Decimal("0")


def get_open_positions() -> set[tuple[str, str]]:
    """
    Get all open positions.

    Returns:
        Set of (symbol, position_side) tuples for non-zero positions
    """
    positions = set()

    try:
        account = get_account_info()
        if not account:
            return positions

        pos_list = account.get("positions") or []
        for pos in pos_list:
            pos_dict = _to_dict(pos) if not isinstance(pos, dict) else pos
            symbol = pos_dict.get("symbol")
            amt_str = pos_dict.get("positionAmt") or pos_dict.get("position_amt") or "0"
            amt = Decimal(str(amt_str))
            side = (pos_dict.get("positionSide") or pos_dict.get("position_side") or
                    ("LONG" if amt > 0 else "SHORT" if amt < 0 else None))

            if symbol and amt != 0 and side:
                positions.add((symbol, side.upper()))

    except Exception as e:
        log.error(f"Failed to get open positions: {e}")

    return positions


def has_existing_position_or_order(symbol: str, position_side: str) -> tuple[bool, str]:
    """
    Check if there's already an existing position or pending order for the symbol and direction.

    This prevents duplicate orders when:
    - User manually placed an order via Binance app
    - Bot already has a pending order

    Args:
        symbol: Trading pair
        position_side: 'LONG' or 'SHORT'

    Returns:
        Tuple of (has_duplicate, reason)
    """
    position_side = position_side.upper()

    # Check for existing position
    pos_amt = get_position_amount(symbol, position_side)
    if pos_amt != 0:
        return (True, f"Already have {position_side} position on {symbol} (qty={pos_amt})")

    # Check for pending orders in same direction
    open_orders = get_open_orders(symbol)
    for order in open_orders:
        order_side = (order.get("positionSide") or order.get("position_side") or "").upper()
        order_type = (order.get("type") or "").upper()

        # Skip exit orders (SL/TP)
        if order_type in ("STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"):
            continue

        # Check if it's an entry order in same direction
        if order_side == position_side:
            order_id = order.get("orderId") or order.get("order_id")
            return (True, f"Already have pending {position_side} order on {symbol} (ID={order_id})")

    return (False, "")


def sync_external_positions() -> int:
    """
    Sync positions that were opened externally (via Binance app) to our state.

    This allows tracking and logging trades even if they weren't placed by the bot.

    Returns:
        Number of positions synced
    """
    from state_store import register_entry_trade, iter_tracked_trades

    synced = 0
    positions = get_open_positions()

    # Get symbols already tracked
    tracked_symbols = set()
    for key, record in iter_tracked_trades():
        symbol = record.get("symbol")
        side = (record.get("position_side") or "").upper()
        if symbol and side:
            tracked_symbols.add((symbol, side))

    # Find positions not in our state
    for symbol, position_side in positions:
        if (symbol, position_side) in tracked_symbols:
            continue

        # Get position details
        pos_amt = get_position_amount(symbol, position_side)
        if pos_amt == 0:
            continue

        # Get current price as entry price estimate
        market_price = get_market_price(symbol)
        if not market_price:
            continue

        log.info(f"Found external position: {symbol} {position_side} qty={pos_amt}")

        # Register with a fake order ID (negative timestamp to distinguish)
        import time
        fake_order_id = -int(time.time() * 1000)

        try:
            register_entry_trade(
                symbol=symbol,
                position_side=position_side,
                order_type="EXTERNAL",
                entry_price=market_price,
                quantity=str(abs(pos_amt)),
                leverage=0,  # Unknown
                stop_loss="",
                take_profit="",
                entry_order_id=fake_order_id,
                channel_title="External (Binance App)",
                raw_signal="Position opened externally",
            )
            synced += 1
            log.info(f"Synced external position: {symbol} {position_side}")
        except Exception as e:
            log.error(f"Failed to sync external position {symbol}: {e}")

    return synced


# =============================================================================
# 6. ORDER MANAGEMENT FUNCTIONS
# =============================================================================

def query_order(
    symbol: str,
    order_id: Optional[int] = None,
    client_order_id: Optional[str] = None
) -> Optional[dict]:
    """
    Query a specific order's status.

    Args:
        symbol: Trading pair
        order_id: Binance order ID
        client_order_id: Client-assigned order ID

    Returns:
        Order info dict or None if not found
    """
    if binance_client is None:
        return None

    try:
        params = {"symbol": symbol}
        if order_id is not None:
            params["order_id"] = order_id
        if client_order_id is not None:
            params["orig_client_order_id"] = client_order_id

        resp = binance_client.rest_api.query_order(**params)
        return _to_dict(resp.data())
    except ClientError as e:
        log.error(f"Failed to query order: {e}")
        return None


def get_open_orders(symbol: Optional[str] = None) -> list[dict]:
    """
    Get all open orders, optionally filtered by symbol.

    Args:
        symbol: Filter by trading pair (None for all)

    Returns:
        List of open order dicts
    """
    if binance_client is None:
        return []

    try:
        params = {"recv_window": 5000}
        if symbol:
            params["symbol"] = symbol

        resp = binance_client.rest_api.current_all_open_orders(**params)
        raw_data = resp.data()

        # Handle response - could be list directly or wrapped
        if isinstance(raw_data, list):
            return [_to_dict(o) if not isinstance(o, dict) else o for o in raw_data]
        else:
            temp = _to_dict(raw_data)
            orders = temp.get("_list") or []
            return [_to_dict(o) if not isinstance(o, dict) else o for o in orders]
    except Exception as e:
        log.error(f"Failed to get open orders: {e}")
        return []


def cancel_order(symbol: str, order_id: int) -> bool:
    """
    Cancel an order safely (no exception on failure).

    Args:
        symbol: Trading pair
        order_id: Order ID to cancel

    Returns:
        True if cancelled successfully, False otherwise
    """
    if binance_client is None:
        return False

    try:
        binance_client.rest_api.cancel_order(symbol=symbol, order_id=order_id)
        log.info(f"Cancelled order {order_id} @ {symbol}")
        return True
    except ClientError as e:
        log.error(f"Failed to cancel order {symbol}/{order_id}: {e}")
        return False
    except Exception as e:
        log.error(f"Unexpected error cancelling {symbol}/{order_id}: {e}")
        return False


def place_order(**params) -> Optional[dict]:
    """
    Place a new order.

    Args:
        **params: Order parameters (symbol, side, type, quantity, etc.)

    Returns:
        Order response dict or None if failed
    """
    if binance_client is None:
        return None

    try:
        resp = binance_client.rest_api.new_order(**params)
        return _to_dict(resp.data())
    except ClientError as e:
        log.error(f"Failed to place order: {e}")
        return None


def get_user_trades(symbol: str, order_id: Optional[int] = None, limit: int = 50) -> list[dict]:
    """
    Get account trade history.

    Args:
        symbol: Trading pair
        order_id: Filter by order ID
        limit: Max number of trades to return

    Returns:
        List of trade dicts
    """
    if binance_client is None:
        return []

    try:
        params = {"symbol": symbol, "limit": limit}
        if order_id:
            params["order_id"] = order_id

        resp = binance_client.rest_api.account_trade_list(**params)
        raw_data = resp.data()

        # Handle response - could be list directly or wrapped
        if isinstance(raw_data, list):
            return [_to_dict(t) if not isinstance(t, dict) else t for t in raw_data]
        else:
            temp = _to_dict(raw_data)
            trades = temp.get("_list") or []
            return [_to_dict(t) if not isinstance(t, dict) else t for t in trades]
    except Exception as e:
        log.error(f"Failed to get user trades: {e}")
        return []


# =============================================================================
# 7. LEVERAGE MANAGEMENT
# =============================================================================

def get_max_leverage(symbol: str) -> int:
    """
    Get the maximum allowed leverage for a symbol.

    Args:
        symbol: Trading pair

    Returns:
        Maximum leverage or DEFAULT_LEVERAGE if lookup fails
    """
    if binance_client is None:
        return int(DEFAULT_LEVERAGE)

    try:
        resp = binance_client.rest_api.notional_and_leverage_brackets(symbol=symbol)
        raw_data = resp.data()

        # SDK returns OneOf type - actual data is in actual_instance
        if hasattr(raw_data, "actual_instance"):
            raw_data = raw_data.actual_instance

        # Handle response - could be list directly or wrapped
        if isinstance(raw_data, list):
            data = raw_data
        else:
            # Try to get list from object
            if hasattr(raw_data, "__iter__") and not isinstance(raw_data, (str, dict)):
                data = list(raw_data)
            else:
                temp = _to_dict(raw_data)
                data = temp.get("_list") or [temp] if temp else []

        if data and len(data) > 0:
            first_item = data[0]

            # Access actual_instance if it's a OneOf wrapper
            if hasattr(first_item, "actual_instance"):
                first_item = first_item.actual_instance

            first_dict = _to_dict(first_item) if not isinstance(first_item, dict) else first_item

            # Get brackets - try attribute first, then dict key
            brackets = []
            if hasattr(first_item, "brackets"):
                brackets = first_item.brackets or []
            if not brackets:
                brackets = first_dict.get("brackets") or []

            max_lev = 0
            for b in brackets:
                # Access actual_instance if wrapper
                if hasattr(b, "actual_instance"):
                    b = b.actual_instance

                b_dict = _to_dict(b) if not isinstance(b, dict) else b

                # Try attribute access first
                if hasattr(b, "initial_leverage"):
                    lev = int(b.initial_leverage or 0)
                else:
                    lev = int(b_dict.get("initialLeverage") or b_dict.get("initial_leverage") or 0)

                if lev > max_lev:
                    max_lev = lev

            if max_lev > 0:
                log.info(f"Max leverage for {symbol}: {max_lev}x")
                return max_lev

    except Exception as e:
        log.error(f"Failed to get max leverage for {symbol}: {e}")

    return int(DEFAULT_LEVERAGE)


def set_leverage(symbol: str, suggested: Optional[int] = None) -> int:
    """
    Set leverage for a symbol, respecting overrides and max limits.

    Args:
        symbol: Trading pair
        suggested: Suggested leverage (can be overridden by config)

    Returns:
        Actual leverage set, or 0 if failed
    """
    if binance_client is None:
        return 0

    # Determine target leverage
    if symbol in LEVERAGE_OVERRIDES:
        target = int(LEVERAGE_OVERRIDES[symbol])
    elif suggested and suggested > 0:
        target = int(suggested)
    else:
        target = int(DEFAULT_LEVERAGE)

    # Cap to max allowed
    max_allowed = get_max_leverage(symbol)
    final_leverage = min(target, max_allowed)

    try:
        log.info(f"Setting {symbol} leverage: {final_leverage}x (max: {max_allowed})")
        binance_client.rest_api.change_initial_leverage(symbol=symbol, leverage=final_leverage)
        return final_leverage

    except ClientError as e:
        if getattr(e, "error_code", None) == -4048:
            # Leverage unchanged (already at target)
            return final_leverage
        log.error(f"Failed to set leverage for {symbol}: {e}")
        return 0
    except Exception:
        return 0


# =============================================================================
# 8. RISK MANAGEMENT (ATR, SL/TP CALCULATION)
# =============================================================================

def compute_atr(klines: list[dict], period: int = 14) -> Optional[Decimal]:
    """
    Calculate Average True Range (ATR) from kline data.

    Args:
        klines: List of kline dicts with high, low, close
        period: ATR period

    Returns:
        ATR value or None if insufficient data
    """
    if len(klines) < period + 1:
        return None

    true_ranges = []
    prev_close = klines[0]["close"]

    for i in range(1, len(klines)):
        high = klines[i]["high"]
        low = klines[i]["low"]

        tr = max(
            high - low,
            abs(high - prev_close),
            abs(prev_close - low)
        )
        true_ranges.append(tr)
        prev_close = klines[i]["close"]

    if len(true_ranges) < period:
        return None

    return sum(true_ranges[-period:]) / Decimal(period)


def compute_sl_tp(
    symbol: str,
    action: str,
    entry_price: Decimal
) -> tuple[Decimal, Decimal]:
    """
    Compute stop-loss and take-profit prices based on ATR.

    Args:
        symbol: Trading pair
        action: 'BUY' or 'SELL'
        entry_price: Entry price

    Returns:
        Tuple of (stop_loss, take_profit) prices
    """
    klines = get_klines(symbol, interval="5m", limit=max(ATR_PERIOD + 20, 60))
    atr = compute_atr(klines, period=ATR_PERIOD)

    min_distance = entry_price * MIN_STOP_DISTANCE_PCT

    if atr is None:
        distance = min_distance
        log.error(f"Cannot compute ATR, using minimum distance: {distance}")
    else:
        distance = max(atr * ATR_K, min_distance)
        log.info(f"ATR={atr:.6f}, distance=max(ATR*{ATR_K}, {MIN_STOP_DISTANCE_PCT*100}%)={distance}")

    is_buy = action.upper() == "BUY"

    if is_buy:
        sl = entry_price - distance
        tp = entry_price + (RR_DEFAULT * distance)
    else:
        sl = entry_price + distance
        tp = entry_price - (RR_DEFAULT * distance)

    return (sl, tp)


def select_sl_tp_with_preference(
    symbol: str,
    action: str,
    entry_price: Decimal,
    user_sl: Optional[str],
    user_tp: Optional[str]
) -> tuple[Decimal, Decimal, list[str]]:
    """
    Select SL/TP values, preferring user-provided values when valid.

    Args:
        symbol: Trading pair
        action: 'BUY' or 'SELL'
        entry_price: Entry price
        user_sl: User-provided stop-loss (or None)
        user_tp: User-provided take-profit (or None)

    Returns:
        Tuple of (stop_loss, take_profit, warnings_list)
    """
    warnings = []
    is_buy = action.upper() == "BUY"

    # Calculate ATR-based minimum distance
    klines = get_klines(symbol, interval="5m", limit=max(ATR_PERIOD + 20, 60))
    atr = compute_atr(klines, period=ATR_PERIOD)
    min_distance = entry_price * MIN_STOP_DISTANCE_PCT

    if atr is None:
        distance_floor = min_distance
        log.error(f"Cannot compute ATR, using min distance: {distance_floor}")
    else:
        distance_floor = max(atr * ATR_K, min_distance)
        log.info(f"ATR={atr:.6f}, min distance={distance_floor}")

    # Validate user SL
    use_user_sl = False
    if user_sl is not None:
        try:
            sl_val = Decimal(str(user_sl))
            sl_distance = abs(entry_price - sl_val)

            # Check direction and minimum distance
            if is_buy and sl_val < entry_price and sl_distance >= distance_floor:
                use_user_sl = True
            elif not is_buy and sl_val > entry_price and sl_distance >= distance_floor:
                use_user_sl = True
            elif sl_distance < distance_floor:
                warnings.append(f"User SL too close ({sl_distance} < {distance_floor}), using computed SL")
            else:
                warnings.append("User SL direction incorrect, using computed SL")
        except Exception:
            warnings.append("User SL parse failed, using computed SL")

    # Get SL
    if use_user_sl:
        sl = Decimal(str(user_sl))
        log.info(f"Using user-provided SL: {sl}")
    else:
        sl, _ = compute_sl_tp(symbol, action, entry_price)
        log.info(f"Using computed SL: {sl}")

    # Validate user TP
    use_user_tp = False
    if user_tp is not None:
        try:
            tp_val = Decimal(str(user_tp))
            if is_buy and tp_val > entry_price:
                use_user_tp = True
            elif not is_buy and tp_val < entry_price:
                use_user_tp = True
        except Exception:
            pass

    # Get TP
    if use_user_tp:
        tp = Decimal(str(user_tp))
        log.info(f"Using user-provided TP: {tp}")
    else:
        # Calculate TP based on RR and SL distance
        if is_buy:
            tp = entry_price + (RR_DEFAULT * (entry_price - sl))
        else:
            tp = entry_price - (RR_DEFAULT * (sl - entry_price))
        log.info(f"Computed TP (RR={RR_DEFAULT}): {tp}")

    # Final sanitization
    try:
        sl, tp, extra_warnings = sanitize_targets(symbol, action, entry_price, sl, tp)
        warnings.extend(extra_warnings)
    except Exception as e:
        warnings.append(f"Sanitization failed, using fallback: {e}")
        sl, tp = compute_sl_tp(symbol, action, entry_price)

    return (sl, tp, warnings)


def sanitize_targets(
    symbol: str,
    action: str,
    entry_price: Decimal,
    stop_loss: Decimal,
    take_profit: Decimal
) -> tuple[Decimal, Decimal, list[str]]:
    """
    Sanitize SL/TP values for validity.

    Args:
        symbol: Trading pair
        action: 'BUY' or 'SELL'
        entry_price: Entry price
        stop_loss: Stop-loss price
        take_profit: Take-profit price

    Returns:
        Tuple of (sanitized_sl, sanitized_tp, warnings_list)
    """
    warnings = []
    is_buy = action.upper() == "BUY"

    e = Decimal(str(entry_price))
    sl = Decimal(str(stop_loss))

    # Validate SL direction
    if is_buy and sl >= e:
        raise ValueError(f"Long SL ({sl}) must be below entry ({e})")
    if not is_buy and sl <= e:
        raise ValueError(f"Short SL ({sl}) must be above entry ({e})")

    # Calculate default TP
    default_tp = e + RR_DEFAULT * (e - sl) if is_buy else e - RR_DEFAULT * (sl - e)

    # Validate TP
    use_default_tp = False
    tp = None

    if take_profit is None:
        use_default_tp = True
    else:
        try:
            tp = Decimal(str(take_profit))
        except Exception:
            use_default_tp = True
            warnings.append("TP parse failed, using default")

        if tp is not None:
            # Check direction
            if (is_buy and tp <= e) or (not is_buy and tp >= e):
                use_default_tp = True

            # Check if TP is unreasonably far
            dist_default = abs(default_tp - e)
            dist_given = abs(tp - e)
            if dist_default > 0 and dist_given > dist_default * RR_MAX:
                use_default_tp = True

    if use_default_tp:
        tp = default_tp
        warnings.append(f"TP adjusted to {tp}")

    # Apply price bounds
    min_price, max_price = get_price_bounds(symbol)
    if min_price and tp < min_price:
        tp = min_price
        warnings.append(f"TP below minPrice, adjusted to {tp}")
    if max_price and tp > max_price:
        tp = max_price
        warnings.append(f"TP above maxPrice, adjusted to {tp}")

    return (sl, tp, warnings)


# =============================================================================
# 9. FORMATTING UTILITIES
# =============================================================================

def format_value(value, precision_str: str, round_mode=ROUND_DOWN) -> str:
    """
    Format a value according to precision string.

    Args:
        value: Value to format
        precision_str: Precision template (e.g., '0.001')
        round_mode: Rounding mode

    Returns:
        Formatted string
    """
    if "." in precision_str:
        decimals = len(precision_str.split(".")[-1].rstrip("0"))
    else:
        decimals = 0

    quantizer = Decimal(f"1e-{decimals}")
    return str(Decimal(str(value)).quantize(quantizer, rounding=round_mode))


def cap_quantity_by_margin(
    ref_price: Decimal,
    leverage: Decimal,
    quantity: Decimal,
    max_margin: Decimal,
    step_size: Decimal,
    min_qty: Decimal
) -> Decimal:
    """
    Cap quantity to fit within initial margin limit.

    Args:
        ref_price: Reference price
        leverage: Leverage
        quantity: Desired quantity
        max_margin: Maximum initial margin allowed
        step_size: Lot size step
        min_qty: Minimum quantity

    Returns:
        Capped quantity (0 if below minimum)
    """
    try:
        max_qty = (max_margin * leverage) / ref_price

        if quantity <= max_qty:
            return quantity

        # Round down to step size
        steps = (max_qty / step_size).to_integral_value(rounding=ROUND_DOWN)
        capped = steps * step_size

        return capped if capped >= min_qty else Decimal("0")
    except Exception:
        return Decimal("0")


# =============================================================================
# 10. EXIT ORDER MANAGEMENT
# =============================================================================

def attach_exit_orders(
    symbol: str,
    position_side: str,
    sl_price: str,
    tp_price: str,
    entry_order_id: Optional[int] = None,
    working_type: str = "MARK_PRICE"
) -> tuple[Optional[int], Optional[int]]:
    """
    Attach stop-loss and take-profit orders to a position.

    Args:
        symbol: Trading pair
        position_side: 'LONG' or 'SHORT'
        sl_price: Stop-loss price
        tp_price: Take-profit price
        entry_order_id: Associated entry order ID
        working_type: Price type for triggers ('MARK_PRICE' or 'CONTRACT_PRICE')

    Returns:
        Tuple of (sl_order_id, tp_order_id)
    """
    if binance_client is None:
        return (None, None)

    close_side = "SELL" if position_side == "LONG" else "BUY"

    # Use new_algo_order for conditional orders (STOP_MARKET, TAKE_PROFIT_MARKET)
    # Since 2025-12-09, these order types are routed to algo order endpoint
    sl_params = {
        "algo_type": "CONDITIONAL",
        "symbol": symbol,
        "side": close_side,
        "type": "STOP_MARKET",
        "position_side": position_side,
        "trigger_price": float(sl_price),
        "close_position": "true",
        "working_type": working_type,
        "price_protect": "true",
    }

    tp_params = {
        "algo_type": "CONDITIONAL",
        "symbol": symbol,
        "side": close_side,
        "type": "TAKE_PROFIT_MARKET",
        "position_side": position_side,
        "trigger_price": float(tp_price),
        "close_position": "true",
        "working_type": working_type,
        "price_protect": "true",
    }

    try:
        log.info("Placing SL order (STOP_MARKET, closePosition=true)...")
        sl_resp = binance_client.rest_api.new_algo_order(**sl_params)
        sl_data = _to_dict(sl_resp.data())
        sl_id = sl_data.get("algoId") or sl_data.get("algo_id") or sl_data.get("orderId") or sl_data.get("order_id")
        log.info(f"SL order placed (ID: {sl_id})")

        log.info("Placing TP order (TAKE_PROFIT_MARKET, closePosition=true)...")
        tp_resp = binance_client.rest_api.new_algo_order(**tp_params)
        tp_data = _to_dict(tp_resp.data())
        tp_id = tp_data.get("algoId") or tp_data.get("algo_id") or tp_data.get("orderId") or tp_data.get("order_id")
        log.info(f"TP order placed (ID: {tp_id})")

        # Update state
        if entry_order_id is not None:
            try:
                update_exits_for_trade(entry_order_id, sl_id, tp_id)
            except Exception as e:
                log.error(f"Failed to update state with SL/TP IDs: {e}")

        return (sl_id, tp_id)

    except ClientError as e:
        log.error(f"Failed to attach SL/TP orders: {e}")
        return (None, None)


# =============================================================================
# 11. ORDER MONITORING
# =============================================================================

async def monitor_entry_order(
    symbol: str,
    order_id: int,
    position_side: str,
    sl_price: str,
    tp_price: str,
    timeout_seconds: int = AUTO_CANCEL_SECONDS,
    poll_interval: int = ORDER_MONITOR_INTERVAL
) -> None:
    """
    Monitor an entry order until filled or timeout.

    This coroutine:
    - Polls order status periodically
    - Attaches SL/TP when order fills
    - Auto-cancels if not filled within timeout

    Args:
        symbol: Trading pair
        order_id: Entry order ID
        position_side: 'LONG' or 'SHORT'
        sl_price: Stop-loss price
        tp_price: Take-profit price
        timeout_seconds: Auto-cancel timeout
        poll_interval: Polling interval in seconds
    """
    key = (symbol, order_id)
    log.info(f"Starting monitor for {symbol} order {order_id}, timeout {timeout_seconds}s")

    exits_attached = False
    start_time = time.time()

    try:
        while True:
            await asyncio.sleep(poll_interval)

            order = query_order(symbol, order_id=order_id)
            if not order:
                continue

            status = str(order.get("status", "")).upper()

            # Handle fill
            if status in ("PARTIALLY_FILLED", "FILLED"):
                if not exits_attached:
                    try:
                        sl_id, tp_id = attach_exit_orders(
                            symbol, position_side, sl_price, tp_price,
                            entry_order_id=order_id
                        )
                        exits_attached = True
                        log.info(f"Order filled ({status}), SL/TP attached")

                        _notify_user(
                            f"Order filled - SL/TP attached\n"
                            f"Symbol: {symbol}\n"
                            f"Status: {status}\n"
                            f"SL: {sl_price} (ID: {sl_id})\n"
                            f"TP: {tp_price} (ID: {tp_id})",
                            loop=_telegram_client.loop if _telegram_client else None
                        )
                    except Exception as e:
                        log.error(f"Failed to attach SL/TP: {e}")

                if status == "FILLED":
                    log.info(f"Order {order_id} fully filled, stopping monitor")
                    _notify_user(
                        f"Entry order filled\nSymbol: {symbol}\nOrderID: {order_id}",
                        loop=_telegram_client.loop if _telegram_client else None
                    )
                    return

            # Handle cancellation
            elif status in ("CANCELED", "EXPIRED", "REJECTED"):
                log.info(f"Order {order_id} status {status}, stopping monitor")
                try:
                    clear_closed_trade(order_id)
                except Exception as e:
                    log.error(f"Failed to clear state: {e}")
                return

            # Check timeout
            elapsed = time.time() - start_time
            if elapsed >= timeout_seconds and status != "FILLED":
                log.info(f"Timeout ({timeout_seconds}s), cancelling order {order_id}...")

                try:
                    binance_client.rest_api.cancel_order(symbol=symbol, order_id=order_id)
                    log.info(f"Cancelled order {order_id}")
                    clear_closed_trade(order_id)

                    _notify_user(
                        f"Order cancelled (timeout)\nSymbol: {symbol}\nOrderID: {order_id}",
                        loop=_telegram_client.loop if _telegram_client else None
                    )
                except ClientError as e:
                    log.error(f"Failed to cancel order: {e}")
                    _notify_user(
                        f"Failed to cancel order\nSymbol: {symbol}\nError: {e}",
                        loop=_telegram_client.loop if _telegram_client else None
                    )
                return

    finally:
        _monitoring_orders.discard(key)


async def monitor_position_closes(poll_interval: int = 60) -> None:
    """
    Monitor tracked positions for closures and log trades.

    This background task:
    - Runs continuously
    - Checks all tracked trades periodically
    - Logs and cleans up when positions close

    Args:
        poll_interval: Check interval in seconds
    """
    log.info(f"Started, polling every {poll_interval}s")

    while True:
        await asyncio.sleep(poll_interval)

        if binance_client is None or not _tracked_trades:
            continue

        for key, record in list(_tracked_trades.items()):
            try:
                entry_id = record.get("entry_order_id") or int(key)
                symbol = record.get("symbol")
                position_side = (record.get("position_side") or "LONG").upper()

                if not symbol:
                    continue

                # Check if entry order was filled
                order = query_order(symbol, order_id=int(entry_id))
                if not order:
                    continue

                status = str(order.get("status", "")).upper()
                if status not in ("FILLED", "PARTIALLY_FILLED"):
                    continue

                # Check if position is closed
                pos_amt = get_position_amount(symbol, position_side)

                if pos_amt == 0:
                    log.info(f"Position closed: {symbol}/{position_side}")

                    _log_closed_trade(entry_id, record, symbol)
                    clear_closed_trade(entry_id)

                    _notify_user(
                        f"Position closed\nSymbol: {symbol}\nSide: {position_side}\nTrade logged",
                        loop=_telegram_client.loop if _telegram_client else None
                    )

            except Exception as e:
                log.error(f"Error processing {key}: {e}")


# =============================================================================
# 12. TRADE LOGGING
# =============================================================================

def _log_closed_trade(entry_id, trade_record: dict, symbol: str) -> None:
    """
    Log a closed trade to the trade log.

    Determines outcome (WIN/LOSS/MANUAL) by checking which exit order filled.

    Args:
        entry_id: Entry order ID
        trade_record: Trade state record
        symbol: Trading pair
    """
    try:
        sl_id = trade_record.get("sl_order_id")
        tp_id = trade_record.get("tp_order_id")

        closing_order_id = None
        outcome = "MANUAL"
        realized_pnl = 0.0
        exit_price = 0.0

        # Check if TP filled (WIN)
        if tp_id:
            tp_order = query_order(symbol, order_id=tp_id)
            if tp_order and tp_order.get("status") == "FILLED":
                closing_order_id = tp_id
                outcome = "WIN"

        # Check if SL filled (LOSS)
        if not closing_order_id and sl_id:
            sl_order = query_order(symbol, order_id=sl_id)
            if sl_order and sl_order.get("status") == "FILLED":
                closing_order_id = sl_id
                outcome = "LOSS"

        # Get PnL from trades
        if closing_order_id:
            trades = get_user_trades(symbol, order_id=closing_order_id)
            if trades:
                realized_pnl = sum(float(t.get("realizedPnl", 0.0)) for t in trades)
                exit_price = trades[-1].get("price", 0.0)

                # Cancel the other exit order
                if closing_order_id == tp_id and sl_id:
                    cancel_order(symbol, sl_id)
                elif closing_order_id == sl_id and tp_id:
                    cancel_order(symbol, tp_id)
        else:
            # Manual close - try to get recent trade
            try:
                recent_trades = get_user_trades(symbol, limit=5)
                if recent_trades:
                    last_trade = recent_trades[-1]
                    realized_pnl = float(last_trade.get("realizedPnl", 0.0))
                    exit_price = last_trade.get("price", 0.0)

                    if realized_pnl > 0:
                        outcome = "WIN"
                    elif realized_pnl < 0:
                        outcome = "LOSS"
                    else:
                        outcome = "DRAW"
            except Exception:
                pass

        # Log to CSV
        log_trade({
            "symbol": symbol,
            "position_side": trade_record.get("position_side"),
            "entry_price": trade_record.get("entry_price"),
            "exit_price": exit_price,
            "quantity": trade_record.get("quantity"),
            "leverage": trade_record.get("leverage"),
            "pnl": realized_pnl,
            "signal_source": trade_record.get("channel_title"),
            "win_loss_draw": outcome,
            "raw_signal": trade_record.get("raw_signal"),
        })

    except Exception as e:
        log.error(f"Failed to log trade (Entry {entry_id}): {e}")


# =============================================================================
# 13. STATE RECOVERY
# =============================================================================

def resume_tracked_trades(event_loop=None) -> None:
    """
    Resume tracking of trades after restart.

    Checks each tracked trade and:
    - Restarts monitoring for unfilled entry orders
    - Attaches missing SL/TP for filled entries
    - Logs and cleans up closed positions
    """
    if binance_client is None:
        log.error("Cannot resume: Binance client not initialized")
        return

    if not _tracked_trades:
        log.info("No trades to resume")
        return

    log.info(f"Resuming {len(_tracked_trades)} tracked trades...")

    for key, record in list(_tracked_trades.items()):
        try:
            entry_id = record.get("entry_order_id") or int(key)
            symbol = record.get("symbol")
            position_side = (record.get("position_side") or "LONG").upper()
            sl_price = record.get("stop_loss")
            tp_price = record.get("take_profit")

            if not symbol or not entry_id:
                clear_closed_trade(key)
                continue

            order = query_order(symbol, order_id=int(entry_id))
            if not order:
                clear_closed_trade(entry_id)
                continue

            status = str(order.get("status", "")).upper()
            order_type = str(order.get("type", "")).upper()

            # Skip cancelled/expired orders
            if status in ("CANCELED", "EXPIRED", "REJECTED"):
                clear_closed_trade(entry_id)
                continue

            # Resume monitoring for unfilled LIMIT orders
            if order_type == "LIMIT" and status in ("NEW", "PARTIALLY_FILLED"):
                key_tuple = (symbol, int(entry_id))

                if key_tuple in _monitoring_orders:
                    log.info(f"Already monitoring {entry_id} ({symbol})")
                elif event_loop and getattr(event_loop, "is_running", lambda: False)():
                    try:
                        _monitoring_orders.add(key_tuple)
                        asyncio.run_coroutine_threadsafe(
                            monitor_entry_order(
                                symbol, int(entry_id), position_side,
                                str(sl_price), str(tp_price),
                                AUTO_CANCEL_SECONDS, ORDER_MONITOR_INTERVAL
                            ),
                            event_loop
                        )
                        log.info(f"Resumed monitoring for {entry_id} ({symbol})")
                    except Exception as e:
                        _monitoring_orders.discard(key_tuple)
                        log.error(f"Failed to resume monitoring {symbol}/{entry_id}: {e}")
                continue

            # Handle filled orders
            if status in ("FILLED", "PARTIALLY_FILLED"):
                pos_amt = get_position_amount(symbol, position_side)

                # Position closed - log and clean up
                if pos_amt == 0:
                    log.info(f"Position closed: {symbol}, logging trade...")
                    _log_closed_trade(entry_id, record, symbol)
                    clear_closed_trade(entry_id)
                    continue

                # Position open - check for missing SL/TP
                open_orders = get_open_orders(symbol)
                has_exit = any(
                    _is_exit_order(o) and
                    (o.get("positionSide") or "").upper() == position_side
                    for o in open_orders
                )

                if has_exit:
                    log.info(f"{symbol}/{entry_id} already has SL/TP orders")
                    continue

                if sl_price is None or tp_price is None:
                    log.error(f"{symbol}/{entry_id} missing SL/TP values, cannot attach")
                    continue

                try:
                    attach_exit_orders(
                        symbol, position_side,
                        str(sl_price), str(tp_price),
                        entry_order_id=entry_id
                    )
                    log.info(f"Attached SL/TP for {symbol}/{entry_id}")
                except Exception as e:
                    log.error(f"Failed to attach SL/TP for {symbol}/{entry_id}: {e}")

        except Exception as e:
            log.error(f"Error resuming trade {key}: {e}")

    log.info("Trade resume complete")


# =============================================================================
# 14. RECONCILIATION
# =============================================================================

def _is_exit_order(order: dict) -> bool:
    """Check if an order is an exit (SL/TP) order."""
    order_type = (order.get("type") or "").upper()
    is_exit_type = order_type in (
        "STOP_MARKET", "TAKE_PROFIT_MARKET", "STOP", "TAKE_PROFIT"
    )

    close_position = str(order.get("closePosition", order.get("closeposition", ""))).lower()
    reduce_only = str(order.get("reduceOnly", "")).lower()

    return is_exit_type and (close_position == "true" or reduce_only == "true")


def _derive_position_side(order: dict, is_exit: bool) -> str:
    """Derive position side from order."""
    ps = (order.get("positionSide") or "").upper()
    if ps in ("LONG", "SHORT"):
        return ps

    side = (order.get("side") or "").upper()

    if is_exit:
        return "SHORT" if side == "BUY" else "LONG"
    return "LONG" if side == "BUY" else "SHORT"


def _get_order_time(order: dict) -> int:
    """Get order creation time in milliseconds."""
    try:
        return int(order.get("time") or order.get("updateTime") or 0)
    except Exception:
        return 0


def reconcile_orders(event_loop=None, timeout_seconds: int = AUTO_CANCEL_SECONDS) -> dict:
    """
    Reconcile orders at startup.

    Cleans up:
    - Stale entry orders past timeout
    - Orphan exit orders without positions

    Args:
        event_loop: Event loop for notifications
        timeout_seconds: Timeout for stale entries

    Returns:
        Summary dict with lists of cancelled orders
    """
    log.info("Starting order reconciliation...")
    summary = {"stale_entries": [], "orphan_exits": []}

    try:
        open_orders = get_open_orders()
        log.info(f"Found {len(open_orders)} open orders")
    except Exception as e:
        log.error(f"Failed to get open orders: {e}")
        return summary

    now_ms = int(time.time() * 1000)
    positions = get_open_positions()

    if RECONCILE_VERBOSE:
        log.info(f"Non-zero positions: {sorted(list(positions))}")

    for order in open_orders:
        try:
            symbol = order.get("symbol")
            order_id = order.get("orderId")

            if not symbol or not order_id:
                continue

            is_exit = _is_exit_order(order)
            pos_side = _derive_position_side(order, is_exit)
            order_type = (order.get("type") or "").upper()
            create_time = _get_order_time(order)

            if RECONCILE_VERBOSE:
                try:
                    log.info(f"Order: {json.dumps(order, ensure_ascii=False)}")
                except Exception:
                    log.info(f"Order: {order}")

            # Handle orphan exit orders
            if is_exit:
                pos_amt = get_position_amount(symbol, pos_side)

                if RECONCILE_VERBOSE:
                    log.info(f"Position ({symbol}, {pos_side}): {pos_amt}")

                if abs(pos_amt) == Decimal("0") or (symbol, pos_side) not in positions:
                    # Try to log the trade first
                    logged = False
                    for key, record in list(_tracked_trades.items()):
                        rec_symbol = record.get("symbol")
                        rec_side = (record.get("position_side") or "").upper()
                        rec_sl_id = record.get("sl_order_id")
                        rec_tp_id = record.get("tp_order_id")

                        if rec_symbol == symbol and rec_side == pos_side:
                            if order_id in (rec_sl_id, rec_tp_id):
                                entry_id = record.get("entry_order_id") or key
                                log.info(f"Found trade for orphan order {order_id}")
                                try:
                                    _log_closed_trade(entry_id, record, symbol)
                                    clear_closed_trade(entry_id)
                                    logged = True
                                except Exception as e:
                                    log.error(f"Failed to log trade: {e}")
                                break

                    if cancel_order(symbol, order_id):
                        summary["orphan_exits"].append({
                            "symbol": symbol,
                            "orderId": order_id,
                            "type": order_type,
                            "positionSide": pos_side,
                            "logged": logged,
                        })

                        _notify_user(
                            f"Cleaned orphan SL/TP\n"
                            f"Symbol: {symbol}\n"
                            f"Type: {order_type}\n"
                            f"Side: {pos_side}\n"
                            f"Logged: {'Yes' if logged else 'No'}",
                            loop=event_loop
                        )
                continue

            # Handle stale entry orders
            if create_time and (now_ms - create_time) >= timeout_seconds * 1000:
                if cancel_order(symbol, order_id):
                    summary["stale_entries"].append({
                        "symbol": symbol,
                        "orderId": order_id,
                        "type": order_type,
                        "positionSide": pos_side,
                    })

                    try:
                        clear_closed_trade(order_id)
                    except Exception as e:
                        if RECONCILE_VERBOSE:
                            log.error(f"Failed to clear state: {e}")

                    _notify_user(
                        f"Cancelled stale order\n"
                        f"Symbol: {symbol}\n"
                        f"Type: {order_type}\n"
                        f"Side: {pos_side}",
                        loop=event_loop
                    )

        except Exception as e:
            if RECONCILE_VERBOSE:
                log.error(f"Error processing order: {e}")

    log.info(
        f"Complete. "
        f"Stale entries: {len(summary['stale_entries'])}, "
        f"Orphan exits: {len(summary['orphan_exits'])}"
    )

    if summary["stale_entries"] or summary["orphan_exits"]:
        _notify_user(
            f"Reconciliation complete\n"
            f"Stale entries cancelled: {len(summary['stale_entries'])}\n"
            f"Orphan SL/TP cancelled: {len(summary['orphan_exits'])}",
            loop=event_loop
        )

    return summary


# =============================================================================
# 15. DAILY SUMMARY
# =============================================================================

def _get_income_records(start_ms: int, end_ms: int, limit: int = 1000) -> list[dict]:
    """Get income/PnL records from Binance."""
    if binance_client is None:
        return []

    try:
        resp = binance_client.rest_api.get_income_history(
            start_time=int(start_ms),
            end_time=int(end_ms),
            limit=int(limit)
        )
        raw_data = resp.data()

        # Handle response - could be list directly or wrapped
        if isinstance(raw_data, list):
            return [_to_dict(r) if not isinstance(r, dict) else r for r in raw_data]
        else:
            temp = _to_dict(raw_data)
            records = temp.get("_list") or []
            return [_to_dict(r) if not isinstance(r, dict) else r for r in records]
    except Exception as e:
        log.error(f"Failed to get income records: {e}")
        return []


def get_daily_pnl_summary(tz_name: str = "Asia/Taipei") -> str:
    """
    Get today's realized PnL summary.

    Args:
        tz_name: Timezone name

    Returns:
        Formatted summary string
    """
    if binance_client is None:
        return "Cannot calculate: Binance client not initialized"

    # Get timezone
    try:
        tz = ZoneInfo(tz_name) if ZoneInfo else None
    except Exception:
        tz = None

    if tz is None:
        class UTC8(datetime.tzinfo):
            def utcoffset(self, dt):
                return timedelta(hours=8)
            def tzname(self, dt):
                return "UTC+08"
            def dst(self, dt):
                return timedelta(0)
        tz = UTC8()

    now = datetime.now(tz)
    start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz)
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    records = _get_income_records(start_ms, end_ms)

    if not records:
        return (
            f"Today's PnL: 0.0000 USDT (no records)\n"
            f"Period: {start.strftime('%Y-%m-%d %H:%M')} ~ {now.strftime('%H:%M')} ({tz_name})"
        )

    # Aggregate
    total = Decimal("0")
    by_type = {}
    by_symbol = {}

    for r in records:
        try:
            amt = Decimal(str(r.get("income", "0")))
        except Exception:
            continue

        total += amt
        income_type = (r.get("incomeType") or "UNKNOWN").upper()
        symbol = r.get("symbol") or "N/A"

        by_type[income_type] = by_type.get(income_type, Decimal("0")) + amt
        by_symbol[symbol] = by_symbol.get(symbol, Decimal("0")) + amt

    # Format output
    def fmt(x):
        return f"{Decimal(str(x)).quantize(Decimal('0.0000'), rounding=ROUND_DOWN)}"

    type_lines = [
        f"  {k}: {fmt(v)}"
        for k, v in sorted(by_type.items(), key=lambda kv: abs(kv[1]), reverse=True)
    ]

    top_symbols = sorted(by_symbol.items(), key=lambda kv: abs(kv[1]), reverse=True)[:6]
    symbol_lines = [f"  {k}: {fmt(v)}" for k, v in top_symbols]

    return (
        f"Today's Realized PnL\n"
        f"Total: {fmt(total)} USDT\n"
        f"Period: {start.strftime('%Y-%m-%d %H:%M')} ~ {now.strftime('%H:%M')} ({tz_name})\n"
        f"\n--- By Type ---\n" + ("\n".join(type_lines) or "  No data") +
        f"\n\n--- Top Symbols ---\n" + ("\n".join(symbol_lines) or "  No data")
    )


async def daily_pnl_notifier(tz_name: str = "Asia/Taipei", hour: int = 12, minute: int = 0) -> None:
    """
    Send daily PnL notification at specified time.

    Args:
        tz_name: Timezone name
        hour: Hour to send notification (24h format)
        minute: Minute to send notification
    """
    try:
        tz = ZoneInfo(tz_name) if ZoneInfo else None
    except Exception:
        tz = None

    if tz is None:
        class UTC8(datetime.tzinfo):
            def utcoffset(self, dt):
                return timedelta(hours=8)
            def tzname(self, dt):
                return "UTC+08"
            def dst(self, dt):
                return timedelta(0)
        tz = UTC8()

    while True:
        now = datetime.now(tz)
        next_run = datetime(now.year, now.month, now.day, hour, minute, 0, tzinfo=tz)

        if now >= next_run:
            next_run += timedelta(days=1)

        wait_seconds = (next_run - now).total_seconds()
        log.info(f"Next notification at {next_run.strftime('%Y-%m-%d %H:%M:%S')} ({int(wait_seconds)}s)")

        await asyncio.sleep(wait_seconds)

        try:
            summary = get_daily_pnl_summary(tz_name)
            _notify_user(summary, loop=_telegram_client.loop if _telegram_client else None)
        except Exception as e:
            log.error(f"Failed to send PnL notification: {e}")


# =============================================================================
# 16. LEGACY ALIASES (for backward compatibility)
# =============================================================================

# These aliases maintain compatibility with existing code
get_binance_market_price = get_market_price
get_binance_klines_raw = get_klines
_get_open_positions_set = get_open_positions
_get_position_amount = get_position_amount
_query_order = query_order
_cancel_order_safely = cancel_order
_sdk_get_open_orders = lambda symbol: get_open_orders(symbol)
_get_all_open_orders = get_open_orders
_attach_exits_after_fill = attach_exit_orders
monitor_and_auto_cancel = monitor_entry_order
reconcile_on_start = reconcile_orders
resume_trades_from_state = resume_tracked_trades
select_sl_tp_with_user_pref = select_sl_tp_with_preference
format_value_by_precision = format_value
cap_qty_by_initial_margin = cap_quantity_by_margin
_handle_closed_trade_logging = _log_closed_trade
