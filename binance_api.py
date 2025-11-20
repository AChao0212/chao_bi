import re
import time
import json
import asyncio
from decimal import Decimal, ROUND_DOWN
from config import (
    DEFAULT_LEVERAGE, LEVERAGE_OVERRIDES,
    BINANCE_API_KEY, BINANCE_API_SECRET, REAL_FUTURES_BASE_URL,
    RR_DEFAULT, RR_MAX, MIN_STOP_DISTANCE_PCT, ATR_K, ATR_PERIOD,
    SLOW_STABLE_RECONCILE, PER_SYMBOL_RETRY, RECONCILE_VERBOSE,
    AUTO_CANCEL_SECONDS, ORDER_MONITOR_INTERVAL, PER_SYMBOL_SLEEP_SEC,
)
from telegram import client, notify_user
from trade_logger import log_trade
from binance.um_futures import UMFutures
from binance.error import ClientError
from datetime import datetime, timedelta
from state_store import _tracked_trades, update_exits_for_trade, clear_closed_trade
try:
    from zoneinfo import ZoneInfo
except Exception:
    ZoneInfo = None

# === [binance_ext] 幣安 API 包裝與工具 ===
# --- 4. 💸 幣安 API 函數 (v32) ---

# 全域變數
total_available_margin = 0.0
binance_client = None
_symbol_max_leverage_cache = {} # 槓桿上限快取

# 追蹤目前已啟動監控的 (symbol, order_id)，避免重複啟動 monitor_and_auto_cancel
_monitoring_orders: set[tuple[str, int]] = set()

def normalize_aliases(text: str) -> str:
    if not text:
        return text
    t = text
    for pat, repl in ALIAS_MAP.items():
        try:
            t = re.sub(pat, repl, t, flags=re.IGNORECASE)
        except Exception:
            pass
    return t
# -------------------------------------------------------------------
# --- 俗稱/別名正規化（將中文俗稱替換為標準代號，方便預過濾與解析） ---
ALIAS_MAP = {
    r"(大餅|比特|比特幣)": "BTC",
    r"(姨太|以太|二餅)": "ETH",
}

def get_symbol_max_leverage(symbol: str) -> int:
    """
    取得該合約允許的最高槓桿。
    嘗試順序：
      1) /fapi/v1/leverageBracket(symbol=...)
      2) /fapi/v1/leverageBracket() 無參數 → 找到相符的 symbol
      3) exchange_info 內的 LEVERAGE filter（若存在）
      4) fallback: DEFAULT_LEVERAGE
    """
    if symbol in _symbol_max_leverage_cache:
        return _symbol_max_leverage_cache[symbol]

    # 1) 正規：leverage_bracket(symbol=...)
    try:
        if binance_client and hasattr(binance_client, "leverage_bracket"):
            lb = binance_client.leverage_bracket(symbol=symbol)
            if isinstance(lb, list) and lb:
                brackets = lb[0].get("brackets", [])
                max_lev = 0
                for b in brackets:
                    try:
                        max_lev = max(max_lev, int(b.get("initialLeverage", 0)))
                    except Exception:
                        continue
                if max_lev > 0:
                    _symbol_max_leverage_cache[symbol] = max_lev
                    return max_lev
    except Exception:
        pass

    # 2) 擴充：leverage_bracket() 無參數 → 找出當前 symbol
    try:
        if binance_client and hasattr(binance_client, "leverage_bracket"):
            lbs = binance_client.leverage_bracket()
            # 介面可能是 list of dict，每個 dict 可能含 symbol/brackets
            if isinstance(lbs, list):
                for item in lbs:
                    try:
                        if (item.get("symbol") or "").upper() == symbol.upper():
                            brackets = item.get("brackets", [])
                            max_lev = 0
                            for b in brackets:
                                try:
                                    max_lev = max(max_lev, int(b.get("initialLeverage", 0)))
                                except Exception:
                                    continue
                            if max_lev > 0:
                                _symbol_max_leverage_cache[symbol] = max_lev
                                return max_lev
                            break
                    except Exception:
                        continue
    except Exception:
        pass

    # 3) 後備：exchange_info 的 LEVERAGE 濾器
    try:
        info = get_symbol_info(symbol)
        if info:
            lev_filter = next((f for f in info.get("filters", []) if f.get("filterType") in ("LEVERAGE", "leverage")), None)
            if lev_filter:
                max_lev = int(lev_filter.get("maxLeverage", DEFAULT_LEVERAGE))
                _symbol_max_leverage_cache[symbol] = max_lev
                return max_lev
    except Exception:
        pass

    # 4) 都失敗：回預設
    _symbol_max_leverage_cache[symbol] = int(DEFAULT_LEVERAGE)
    return int(DEFAULT_LEVERAGE)

def apply_leverage_override(symbol: str, suggested: int | None) -> int:
    """
    先依 LEVERAGE_OVERRIDES 覆寫；若無則用 LLM/預設。
    不在此處預先以交易所上限裁切，讓 set_binance_leverage() 先嘗試，
    若超過才由該函式依 -4028 錯誤回退。
    """
    if symbol in LEVERAGE_OVERRIDES:
        lev = int(LEVERAGE_OVERRIDES[symbol])
    elif suggested is None:
        lev = int(DEFAULT_LEVERAGE)
    else:
        lev = int(suggested)
    return lev

# 在腳本頂層初始化幣安客戶端
if not BINANCE_API_KEY or not BINANCE_API_SECRET:
    print("[Binance] [error]: 找不到 'binance.txt' 或金鑰不完整。")
else:
    try:
        binance_client = UMFutures(
            key=BINANCE_API_KEY, 
            secret=BINANCE_API_SECRET, 
            base_url=REAL_FUTURES_BASE_URL
        )
        
        try:
            position_mode = binance_client.get_position_mode()
            if position_mode.get('dualSidePosition') == False:
                print("[Binance] [warning]: 偵測到帳戶為「單向持倉」，正在嘗試切換至「雙向持倉」...")
                binance_client.change_position_mode(dualSidePosition=True)
                print("[Binance 資訊]：已成功切換至「雙向持倉 (Hedge Mode)」。")
            else:
                print("[Binance 資訊]：帳戶已處於「雙向持倉 (Hedge Mode)」。")
        except ClientError as e:
            if e.error_code == -4059: # "No need to change position side."
                print("[Binance 資訊]：帳戶已處於「雙向持倉 (Hedge Mode)」。")
            else:
                raise 
        
        account_info = binance_client.account()
        total_available_margin = float(account_info['availableBalance'])
        
        if total_available_margin <= 0:
             print(f"[Binance] [error]: 總可用保證金 (availableBalance) 為 0。")
             binance_client = None
        else:
            print(f"[Binance] [info]: 幣安 *真實環境* 連接成功！")
            print(f"   多幣種保證金 總可用餘額 (availableBalance): {total_available_margin} USDT")

    except ClientError as e:
        print(f"[Binance] [error]: API Key 或 Secret 錯誤。{e}")
        binance_client = None
    except Exception as e:
        print(f"[Binance] [error]: 連接失敗: {e}")
        binance_client = None


symbol_info_cache = {} 

def get_symbol_info(symbol):
    """(此函數不變)"""
    if symbol in symbol_info_cache:
        return symbol_info_cache[symbol]
    if binance_client is None: return None
    try:
        info = binance_client.exchange_info()
        for item in info['symbols']:
            if item['symbol'] == symbol:
                symbol_info_cache[symbol] = item
                return item
        print(f"[Binance] [error]: 找不到 {symbol} 的交易對資訊")
        return None
    except ClientError as e:
        print(f"[Binance] [error]: 獲取 Exchange Info 失敗: {e}")
        return None

# --- 檢查 symbol 是否有效 ---
def is_valid_symbol(symbol: str) -> bool:
    """
    檢查交易對是否存在於 exchange_info（支援中文合約名稱）。
    """
    try:
        return get_symbol_info(symbol) is not None
    except Exception:
        return False

def get_binance_market_price(symbol):
    """(此函數不變)"""
    if binance_client is None: return None
    try:
        ticker = binance_client.ticker_price(symbol)
        return ticker['price']
    except ClientError as e:
        print(f"[Binance] [error]: 獲取 {symbol} 市價失敗: {e}")
        return None

def get_binance_klines_for_llm(symbol, interval='5m', limit=50):
    """(此函數不變)"""
    if binance_client is None: return "K-line data not available."
    
    interval_map = {'1h': '1h', '4h': '4h', '1d': '1d', '5m': '5m'}
    klines_string = "Timestamp, Open, High, Low, Close, Volume\n"
    
    try:
        print(f"[Binance] [info]: 正在獲取 {symbol} 最近 {limit} 根 {interval} K線...")
        klines = binance_client.klines(
            symbol=symbol,
            interval=interval_map.get(interval, '5m'), 
            limit=limit
        )
        for k in klines:
            timestamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(k[0]/1000))
            klines_string += f"{timestamp}, {k[1]}, {k[2]}, {k[3]}, {k[4]}, {k[5]}\n"
        return klines_string
    except ClientError as e:
        print(f"[Binance] [error]: 獲取 {symbol} K 線失敗: {e}")
        return "K-line data not available."

def get_binance_klines_raw(symbol, interval='5m', limit=200):
    """取得數值化 K 線：回傳 list(dict) with keys: open, high, low, close."""
    if binance_client is None:
        return []
    try:
        klines = binance_client.klines(symbol=symbol, interval=interval, limit=limit)
        out = []
        for k in klines:  # [open_time, open, high, low, close, volume, close_time, ...]
            out.append({
                "open": Decimal(k[1]),
                "high": Decimal(k[2]),
                "low":  Decimal(k[3]),
                "close":Decimal(k[4]),
            })
        return out
    except ClientError as e:
        print(f"[Binance] [error]: 取得 {symbol} 原始 K 線失敗: {e}")
        return []

def compute_atr_from_klines(klines, period=14):
    """純 Python 計 ATR；需要至少 period+1 根 K 線。"""
    if len(klines) < period + 1:
        return None
    trs = []
    prev_close = klines[0]["close"]
    for i in range(1, len(klines)):
        high = klines[i]["high"]
        low  = klines[i]["low"]
        tr = max(high - low, abs(high - prev_close), abs(prev_close - low))
        trs.append(tr)
        prev_close = klines[i]["close"]
    if len(trs) < period:
        return None
    atr = sum(trs[-period:]) / Decimal(period)
    return atr

def compute_sl_tp_python(symbol, action, entry_price_dec):
    """
    以 ATR 與最小百分比距離計算止損與止盈（RR = 1.5）。
    BUY:  SL = entry - dist；TP = entry + 1.5*dist
    SELL: SL = entry + dist；TP = entry - 1.5*dist
    """
    k_raw = get_binance_klines_raw(symbol, interval='5m', limit=max(ATR_PERIOD + 20, 60))
    atr = compute_atr_from_klines(k_raw, period=ATR_PERIOD)
    min_pct_dist = (entry_price_dec * MIN_STOP_DISTANCE_PCT)
    if atr is None:
        dist = min_pct_dist
        print(f"⚠️ 無法計算 ATR，使用最小百分比距離: {dist}")
    else:
        dist = max(atr * ATR_K, min_pct_dist)
        print(f"   [Risk-Py] ATR={atr:.6f}，距離採用 max(ATR*{ATR_K}, {MIN_STOP_DISTANCE_PCT*100}%) = {dist}")

    if action.upper() == 'BUY':
        sl = entry_price_dec - dist
        tp = entry_price_dec + (RR_DEFAULT * dist)
    else:
        sl = entry_price_dec + dist
        tp = entry_price_dec - (RR_DEFAULT * dist)

    return (sl, tp)


# --- 新增 helper: select_sl_tp_with_user_pref ---
def select_sl_tp_with_user_pref(symbol, action, entry_price_dec, user_sl_str, user_tp_str):
    """
    遵從使用者/訊號給的 SL/TP（若有效），否則 fallback 到 Python 風控算法。
    規則：
    • SL 若提供且方向正確，且距離 ≥ min_stop（max(ATR*ATR_K, MIN_STOP_DISTANCE_PCT)），則採用使用者 SL。
    • 否則用 compute_sl_tp_python() 產生的 SL。
    • TP 若提供且方向正確，則保留；若未提供或方向錯誤，依 RR_DEFAULT 與最終 SL 計算。
    回傳 (sl_decimal, tp_decimal, warnings_list)
    """
    warnings = []
    is_buy = action.upper() == 'BUY'

    # 先計算 ATR 與最小距離基準
    k_raw = get_binance_klines_raw(symbol, interval='5m', limit=max(ATR_PERIOD + 20, 60))
    atr = compute_atr_from_klines(k_raw, period=ATR_PERIOD)
    min_pct_dist = (entry_price_dec * MIN_STOP_DISTANCE_PCT)
    if atr is None:
        dist_floor = min_pct_dist
        print(f"⚠️ 無法計算 ATR，使用最小百分比距離: {dist_floor}")
    else:
        dist_floor = max(atr * ATR_K, min_pct_dist)
        print(f"   [Risk-Py] ATR={atr:.6f}，距離下限採用 max(ATR*{ATR_K}, {MIN_STOP_DISTANCE_PCT*100}%) = {dist_floor}")

    # 嘗試採用使用者 SL
    use_user_sl = False
    if user_sl_str is not None:
        try:
            user_sl = Decimal(str(user_sl_str))
            if is_buy and user_sl < entry_price_dec:
                if (entry_price_dec - user_sl) >= dist_floor:
                    use_user_sl = True
                else:
                    warnings.append(f"使用者 SL 距離過近（{entry_price_dec - user_sl} < {dist_floor}），改用程式計算")
            elif (not is_buy) and user_sl > entry_price_dec:
                if (user_sl - entry_price_dec) >= dist_floor:
                    use_user_sl = True
                else:
                    warnings.append(f"使用者 SL 距離過近（{user_sl - entry_price_dec} < {dist_floor}），改用程式計算")
            else:
                warnings.append("使用者 SL 方向錯誤，改用程式計算")
        except Exception:
            warnings.append("使用者 SL 解析失敗，改用程式計算")

    if use_user_sl:
        sl_dec = user_sl
        print(f"   [Risk-Py] 沿用使用者提供的 SL: {sl_dec}")
    else:
        sl_dec, _tp_tmp = compute_sl_tp_python(symbol, action, entry_price_dec)
        print(f"   [Risk-Py] 採用程式計算 SL: {sl_dec}")

    # 決定 TP：若使用者 TP 有給且方向正確就保留，否則用 RR_DEFAULT 與最終 SL 推出
    use_user_tp = False
    if user_tp_str is not None:
        try:
            user_tp = Decimal(str(user_tp_str))
            if is_buy and user_tp > entry_price_dec:
                use_user_tp = True
            elif (not is_buy) and user_tp < entry_price_dec:
                use_user_tp = True
        except Exception:
            pass

    if use_user_tp:
        tp_dec = user_tp
        print(f"   [Risk-Py] 沿用使用者提供的 TP: {tp_dec}")
    else:
        # 以 RR_DEFAULT 與最終 SL 的距離計算 TP
        if is_buy:
            tp_dec = entry_price_dec + (RR_DEFAULT * (entry_price_dec - sl_dec))
        else:
            tp_dec = entry_price_dec - (RR_DEFAULT * (sl_dec - entry_price_dec))
        print(f"   [Risk-Py] 依 RR_DEFAULT 重新計算 TP: {tp_dec}")

    # 最終經 sanitize，校正邊界與離譜 TP（不會推翻有效方向的 SL）
    try:
        sl_out, tp_out, warn2 = sanitize_targets(symbol, action, entry_price_dec, sl_dec, tp_dec)
        warnings.extend(warn2)
        return (sl_out, tp_out, warnings)
    except Exception as e:
        # 若 sanitize 失敗，退回保守方案：用 compute_sl_tp_python 產生
        warnings.append(f"sanitize 失敗，回退程式 SL/TP：{e}")
        sl_fallback, tp_fallback = compute_sl_tp_python(symbol, action, entry_price_dec)
        return (sl_fallback, tp_fallback, warnings)


def format_value_by_precision(value, precision_str, round_mode=ROUND_DOWN):
    """(此函數不變)"""
    if '.' in precision_str:
        num_decimals = len(precision_str.split('.')[-1].rstrip('0'))
    else:
        num_decimals = 0
    quantizer = Decimal('1e-' + str(num_decimals))
    return str(Decimal(str(value)).quantize(quantizer, rounding=round_mode))

# --- 新增 helper: 取得 LOT_SIZE 濾器 ---
def _get_lot_size_filter(symbol_info: dict):
    """
    從 exchange_info 的單一 symbol 資訊中取出 LOT_SIZE 濾器（含 stepSize/minQty/maxQty）。
    若找不到回傳 None。
    """
    try:
        return next(f for f in symbol_info['filters'] if f.get('filterType') == 'LOT_SIZE')
    except Exception:
        return None


# --- 新增 helper: 以初始保證金上限做最終硬封頂 ---
from math import floor
def _cap_qty_by_initial_margin(ref_price_dec: Decimal, lev_dec: Decimal,
                               qty_dec: Decimal, max_margin_amt: Decimal,
                               step_dec: Decimal, min_qty_dec: Decimal) -> Decimal:
    """
    將數量以『初始保證金上限』做最終硬封頂：
      max_qty = (max_margin_amt * lev_dec) / ref_price_dec
    若 qty_dec > max_qty，向下取整到 step 後回傳；
    若跌破 min_qty，回傳 Decimal('0') 表示不應下單（交由上層取消）。
    """
    try:
        max_qty = (max_margin_amt * lev_dec) / ref_price_dec
        if qty_dec <= max_qty:
            return qty_dec
        # floor to step
        steps = (max_qty / step_dec).to_integral_value(rounding=ROUND_DOWN)
        capped = steps * step_dec
        if capped < min_qty_dec:
            return Decimal('0')
        return capped
    except Exception:
        # 若計算失敗，保守回傳 0 讓上層取消
        return Decimal('0')

def _get_price_bounds(symbol):
    """從 exchange_info 取得此交易對的價格邊界，用於基本 sanity check。"""
    info = get_symbol_info(symbol)
    if not info:
        return (None, None)
    try:
        pf = next(f for f in info['filters'] if f['filterType'] == 'PRICE_FILTER')
        min_price = Decimal(pf.get('minPrice', '0'))
        max_price = Decimal(pf.get('maxPrice', '0'))
        # 有些合約 maxPrice 可能標 0 (表示未限制)
        if max_price == 0:
            max_price = None
        if min_price == 0:
            min_price = None
        return (min_price, max_price)
    except Exception:
        return (None, None)

def sanitize_targets(symbol, action, entry_price, stop_loss, take_profit):
    """
    矯正 SL/TP：方向、合理距離、價格邊界、避免「立即觸發」。
    回傳 (sl_decimal, tp_decimal, warnings_list)
    """
    warnings = []
    is_buy = action.upper() == "BUY"

    e = Decimal(str(entry_price))
    sl = Decimal(str(stop_loss))

    # 方向檢查：若錯誤則直接拋出（外層已有邏輯可攔）
    if is_buy and sl >= e:
        raise ValueError(f"多單止損({sl})不可高於/等於入場({e})")
    if (not is_buy) and sl <= e:
        raise ValueError(f"空單止損({sl})不可低於/等於入場({e})")

    # 預設 TP（固定 RR = 1.5）
    default_tp = (e + RR_DEFAULT * (e - sl)) if is_buy else (e - RR_DEFAULT * (sl - e))

    # 方向/離譜檢查
    use_default = False
    if take_profit is None:
        use_default = True
    else:
        tp_dec = Decimal(str(take_profit))
        if is_buy and tp_dec <= e:
            use_default = True
        if (not is_buy) and tp_dec >= e:
            use_default = True
        # 距離檢查：若 LLM 給的距離 > default 距離 * RR_MAX，視為離譜
        dist_default = abs(default_tp - e)
        dist_given = abs(tp_dec - e)
        if dist_default > 0 and dist_given > dist_default * RR_MAX:
            use_default = True

    tp = default_tp if use_default else Decimal(str(take_profit))
    if use_default:
        warnings.append(f"TP 已重算為 {tp}（矯正離譜或方向錯誤的數值）")

    # 交易對價格邊界檢查（若有）
    min_price, max_price = _get_price_bounds(symbol)
    if min_price is not None and tp < min_price:
        tp = min_price
        warnings.append(f"TP 低於 minPrice，已調整為 {tp}")
    if max_price is not None and tp > max_price:
        tp = max_price
        warnings.append(f"TP 高於 maxPrice，已調整為 {tp}")

    return (sl, tp, warnings)

def set_binance_leverage(symbol, leverage):
    """設定槓桿；回傳實際設定成功的倍數 (int)。若失敗回傳 0。若因 -4028 觸發回退則回傳回退後的倍數。"""
    if binance_client is None:
        return 0

    def _try_set(lv: int):
        try:
            print(f"[Binance] 正在設定 {symbol} 的槓桿為 {lv}x...")
            binance_client.change_leverage(symbol=symbol, leverage=int(lv))
            print(f"[Binance] {symbol} 槓桿已設定為 {lv}x")
            return lv
        except ClientError as e_inner:
            # 已是該值或不須更改
            if getattr(e_inner, "error_code", None) == -4048:
                print(f"[Binance] 槓桿已是 {lv}x 或無需更改。")
                return lv
            # 其他錯誤讓上層處理
            raise e_inner

    try:
        # 先嘗試直接設定
        res = _try_set(int(leverage))
        if res:
            return res
    except ClientError as e:
        # 若超出允許上限（-4028），進入回退流程
        if getattr(e, "error_code", None) == -4028:
            print(f"   [Binance 資訊] 收到 -4028：{symbol} 不允許 {leverage}x，啟動回退流程…")
            # 1) 先查詢上限（強化版）
            max_allowed = 0
            try:
                max_allowed = int(get_symbol_max_leverage(symbol))
            except Exception:
                max_allowed = 0

            # 建立候選清單：先放查到的上限，再放常見可用倍數（遞減）
            trial_candidates = []
            if max_allowed > 0:
                trial_candidates.append(max_allowed)

            # 常見倍數（含一些交易所常見階梯）
            common_desc = [125, 100, 75, 50, 40, 30, 25, 20, 10, 5, 3, 2, 1]
            trial_candidates.extend(common_desc)

            # 去重、過濾高於原請求值的，以及非正整數
            asked = int(leverage)
            dedup = []
            for lv in trial_candidates:
                try:
                    lv_i = int(lv)
                    if lv_i <= 0:
                        continue
                    if lv_i > asked:
                        continue
                    if lv_i not in dedup:
                        dedup.append(lv_i)
                except Exception:
                    continue

            # 逐一嘗試直到成功
            for lv in dedup:
                try:
                    res2 = _try_set(lv)
                    if res2:
                        if lv != asked:
                            print(f"   [Binance 資訊] 已使用回退倍數 {lv}x 取代原請求 {asked}x。")
                        return lv
                except ClientError as ee:
                    # 仍可能 -4028 或其他錯誤，繼續往下嘗試
                    if getattr(ee, "error_code", None) not in (-4028, -4048):
                        # 非預期錯誤：印出並繼續嘗試下一個
                        print(f"   ⚠️ 設定 {lv}x 失敗：{ee}")

            print(f"❌❌❌ 槓桿設定失敗：已嘗試上限與常見倍數仍未成功（symbol={symbol}, requested={leverage}x）。❌❌❌")
            return 0

        # 非 -4028：若已是該值或無需更改，視為成功
        if getattr(e, "error_code", None) == -4048:
            print(f"   [Binance 資訊] 槓桿已是 {leverage}x 或無需更改。")
            return int(leverage)

        print(f"❌❌❌ 槓桿設定失敗：幣安 API 錯誤: {e} ❌❌❌")
        return 0
    except Exception as e:
        print(f"❌❌❌ 槓桿設定失敗：未知錯誤: {e} ❌❌❌")
        return 0

def _query_order(symbol, order_id=None, client_order_id=None):
    """查詢單一訂單狀態（REST），回傳 dict。"""
    if binance_client is None: 
        return None
    try:
        params = {"symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if client_order_id is not None:
            params["origClientOrderId"] = client_order_id
        return binance_client.query_order(**params)
    except ClientError as e:
        print(f"❌ [Binance 錯誤]: 查詢訂單失敗: {e}")
        return None
    
def _get_open_positions_set():
    """
    取得目前持倉集合：回傳 set{ (symbol, positionSide) }，僅包含部位數量不為 0 的倉位。
    Hedge Mode 下，positionSide 會是 'LONG' 或 'SHORT'。
    """
    s = set()
    try:
        info = binance_client.account()
        positions = info.get('positions', [])
        for p in positions:
            symbol = p.get('symbol')
            amt = Decimal(p.get('positionAmt', '0'))
            side = p.get('positionSide') or ('LONG' if amt > 0 else 'SHORT' if amt < 0 else None)
            if symbol and amt != 0 and side:
                s.add((symbol, side))
    except Exception as e:
        print(f"⚠️ 讀取當前持倉失敗：{e}")
    return s

# --- 新增: 取得單一持倉數量 ---
def _get_position_amount(symbol: str, position_side: str):
    """
    取得指定 symbol 與 positionSide ('LONG'/'SHORT') 的 positionAmt (Decimal)。
    若找不到或錯誤，回傳 Decimal('0')。
    """
    try:
        info = binance_client.account()
        positions = info.get('positions', [])
        for p in positions:
            if p.get('symbol') == symbol and (p.get('positionSide') or '').upper() == position_side.upper():
                return Decimal(p.get('positionAmt', '0'))
        return Decimal('0')
    except Exception as e:
        print(f"⚠️ _get_position_amount 讀取失敗: {e}")
        return Decimal('0')

def _cancel_order_safely(symbol, order_id):
    """安全撤單：失敗不丟例外，只印錯誤。優先用低階 DELETE，失敗再用 SDK。"""
    try:
        # 優先用低階 DELETE
        if hasattr(binance_client, 'sign_request'):
            res = binance_client.sign_request('DELETE', '/fapi/v1/order', {'symbol': symbol, 'orderId': order_id})
            print(f"   ✅ 已撤單 {order_id} @ {symbol} (low-level DELETE)")
            return True
        elif hasattr(binance_client, '_request'):
            res = binance_client._request('DELETE', '/fapi/v1/order', True, data={'symbol': symbol, 'orderId': order_id})
            print(f"   ✅ 已撤單 {order_id} @ {symbol} (low-level DELETE)")
            return True
        else:
            # fallback
            binance_client.cancel_order(symbol=symbol, orderId=order_id)
            print(f"   ✅ 已撤單 {order_id} @ {symbol}")
            return True
    except ClientError as e:
        print(f"   ❌ 撤單失敗（{symbol}/{order_id}）：{e}")
    except Exception as e:
        print(f"   ❌ 撤單未知錯誤（{symbol}/{order_id}）：{e}")
    return False

def _list_all_active_symbols():
    """
    列出期貨可交易且活躍(TRADING)的所有 symbol（PERPETUAL / 季度）。
    不再過濾非 ASCII 名稱，因有像「币安人生USDT」這類中文合約。
    """
    syms = []
    try:
        info = binance_client.exchange_info()
        for s in info.get('symbols', []):
            ct = s.get('contractType')
            sym = s.get('symbol')
            if ct in ('PERPETUAL', 'CURRENT_QUARTER', 'NEXT_QUARTER') and s.get('status') == 'TRADING':
                if sym:
                    syms.append(sym)
    except Exception as e:
        print(f"⚠️ 讀取 exchange_info 失敗，無法列出全部 symbol：{e}")
    return syms

# --- Futures 低階 API: 直接簽名 GET ---
 # 注意：因雲端 WAF/404 問題，reconcile 流程暫不使用此低階呼叫。
def _fapi_signed_get(path: str, payload: dict | None = None):
    """
    以最低層的 sign_request 呼叫期貨 REST，避免 SDK 方法名差異。
    path 例如：'/fapi/v1/openOrders' 或 '/fapi/v1/allOpenOrders'
    """
    try:
        if hasattr(binance_client, 'sign_request'):
            return binance_client.sign_request('GET', path, payload or {})
        # 某些舊版命名 _request(method, path, signed=True, data=...)
        if hasattr(binance_client, '_request'):
            return binance_client._request('GET', path, True, data=(payload or {}))
    except Exception as e:
        raise e
    raise AttributeError("UMFutures client lacks sign_request/_request low-level methods.")

# --- Income / PnL helpers (daily summary) ---
def _get_income_records(start_ms: int, end_ms: int, limit: int = 1000):
    """
    Low-level fetch of income history within [start_ms, end_ms).
    Returns a list of income records. Falls back to empty list on error.
    """
    try:
        payload = {'startTime': int(start_ms), 'endTime': int(end_ms), 'limit': int(limit)}
        recs = _fapi_signed_get('/fapi/v1/income', payload)
        if isinstance(recs, list):
            return recs
        return []
    except Exception as e:
        print(f"⚠️ 讀取收入紀錄失敗：{e}")
        return []

def _format_usdt(x) -> str:
    try:
        return f"{Decimal(str(x)).quantize(Decimal('0.0000'), rounding=ROUND_DOWN)}"
    except Exception:
        return str(x)

def get_today_pnl_summary(tz_name: str = 'Asia/Taipei') -> str:
    """
    計算【本地時區】當日 00:00 至目前為止的已實現損益彙總（不含未實現）。
    來源：/fapi/v1/income（REALIZED_PNL、COMMISSION、FUNDING_FEE…）
    """
    if binance_client is None:
        return "❌ 無法計算：幣安客戶端未初始化。"

    try:
        tz = ZoneInfo(tz_name) if ZoneInfo else None
    except Exception:
        tz = None
    # Fallback：若系統無 zoneinfo，改用 UTC+8
    if tz is None:
        class _TZ8(datetime.tzinfo):
            def utcoffset(self, dt): return timedelta(hours=8)
            def tzname(self, dt): return "UTC+08"
            def dst(self, dt): return timedelta(0)
        tz = _TZ8()

    now = datetime.now(tz)
    start = datetime(now.year, now.month, now.day, 0, 0, 0, tzinfo=tz)
    end = now
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    recs = _get_income_records(start_ms, end_ms)
    if not recs:
        return f"📊 今日盈虧：0.0000 USDT（無紀錄）\n時段：{start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%H:%M')} ({tz_name})"

    # 累計
    total = Decimal('0')
    by_type = {}
    by_symbol = {}

    for r in recs:
        try:
            amt = Decimal(str(r.get('income', '0')))
        except Exception:
            continue
        total += amt
        itype = (r.get('incomeType') or 'UNKNOWN').upper()
        sym = r.get('symbol') or 'N/A'
        by_type[itype] = by_type.get(itype, Decimal('0')) + amt
        by_symbol[sym] = by_symbol.get(sym, Decimal('0')) + amt

    # 排序僅取前幾個重點
    type_lines = []
    for k, v in sorted(by_type.items(), key=lambda kv: abs(kv[1]), reverse=True):
        type_lines.append(f"• {k}: {_format_usdt(v)}")

    top_syms = sorted(by_symbol.items(), key=lambda kv: abs(kv[1]), reverse=True)[:6]
    sym_lines = [f"• {k}: {_format_usdt(v)}" for k, v in top_syms]

    msg = (
        f"📊 今日已實現盈虧（到目前為止）\n"
        f"總計：{_format_usdt(total)} USDT\n"
        f"時段：{start.strftime('%Y-%m-%d %H:%M')} ~ {end.strftime('%H:%M')} ({tz_name})\n"
        f"— 類型拆解 —\n" + ("\n".join(type_lines) if type_lines else "• 無資料") + "\n"
        f"— 主要標的 —\n" + ("\n".join(sym_lines) if sym_lines else "• 無資料")
    )
    return msg

async def _sleep_until(target_dt: datetime):
    """Async sleep until target_dt (aware)."""
    try:
        now = datetime.now(target_dt.tzinfo)
    except Exception:
        now = datetime.now()
    delta = (target_dt - now).total_seconds()
    if delta > 0:
        await asyncio.sleep(delta)

async def daily_pnl_notifier(tz_name: str = 'Asia/Taipei', hour: int = 12, minute: int = 0):
    """
    每日固定時間（預設 12:00 當地時間）回報本日盈虧。
    使用 notify_user() 推送到 NOTIFY_TARGET 或 Saved Messages。
    """
    try:
        tz = ZoneInfo(tz_name) if ZoneInfo else None
    except Exception:
        tz = None
    if tz is None:
        class _TZ8(datetime.tzinfo):
            def utcoffset(self, dt): return timedelta(hours=8)
            def tzname(self, dt): return "UTC+08"
            def dst(self, dt): return timedelta(0)
        tz = _TZ8()

    while True:
        now = datetime.now(tz)
        # 設定下一個觸發時間（今日 12:00；若已過，改為明日）
        next_run = datetime(now.year, now.month, now.day, hour, minute, 0, tzinfo=tz)
        if now >= next_run:
            next_run = next_run + timedelta(days=1)
        # 睡到時間點
        secs = (next_run - now).total_seconds()
        print(f"⏰ PnL 通知排程：將於 {next_run.strftime('%Y-%m-%d %H:%M:%S %Z')} 執行（{int(secs)}s 後）")
        await _sleep_until(next_run)
        # 計算與通知
        try:
            summary = get_today_pnl_summary(tz_name)
            notify_user(summary, loop=client.loop if client else None)
        except Exception as e:
            print(f"⚠️ 發送 PnL 通知失敗：{e}")
        # 下一輪循環

# --- 新增: 兼容不同 binance-connector 版本的 open orders 查詢 ---
def _sdk_get_open_orders(symbol: str):
    """
    穩定版 open orders 讀取：
    • 先用低階 REST: /fapi/v1/openOrders?symbol=...
      （避免部分 binance-connector 版本把 open_orders 綁到 query_order 導致
       'orderId is mandatory' 的錯誤）
    • 若低階呼叫意外失敗，再嘗試 SDK: get_open_orders/open_orders
    """
    # 先走低階；加上 recvWindow 增加容忍度
    try:
        return _fapi_signed_get('/fapi/v1/openOrders', {'symbol': symbol, 'recvWindow': 5000})
    except Exception as low_e:
        # 低階失敗才嘗試 SDK 變體
        pass

    # SDK 變體 1：新版多為 get_open_orders
    if hasattr(binance_client, 'get_open_orders'):
        try:
            return binance_client.get_open_orders(symbol=symbol)
        except Exception as e:
            # 若遇到 "orderId is mandatory"（某些版本錯綁到 query_order），繼續 fallback
            if 'orderId is mandatory' not in str(e):
                raise

    # SDK 變體 2：部分舊版或分支使用 open_orders
    if hasattr(binance_client, 'open_orders'):
        try:
            return binance_client.open_orders(symbol=symbol)
        except Exception as e:
            if 'orderId is mandatory' not in str(e):
                raise

    # 最後一層：再嘗試一次低階，若仍失敗就讓上層重試/記錄
    return _fapi_signed_get('/fapi/v1/openOrders', {'symbol': symbol, 'recvWindow': 5000})

def _get_all_open_orders():
    """
    取得所有未成交訂單：
    • 若 SLOW_STABLE_RECONCILE=True，改用『逐 symbol + SDK open_orders()』的慢速穩定版本
      並在每個 symbol 間 sleep，必要時重試，避免 WAF/風控擋下。
    • 若為 False，才嘗試低階 allOpenOrders/openOrders（較快但容易 404/被擋）。
    """
    if SLOW_STABLE_RECONCILE:
        print("[Reconcile] SLOW mode: 逐 symbol 掃描 open orders（SDK），這會比較慢但更穩…")
        results = []
        symbols = _list_all_active_symbols()
        for sym in symbols:
            for _try in range(PER_SYMBOL_RETRY + 1):
                try:
                    fetched = _sdk_get_open_orders(sym)
                    if isinstance(fetched, list) and fetched:
                        results.extend(fetched)
                    break
                except Exception as e:
                    if _try >= PER_SYMBOL_RETRY:
                        if RECONCILE_VERBOSE:
                            print(f"⚠️ 取 {sym} open orders 失敗（放棄）：{e}")
                    else:
                        if RECONCILE_VERBOSE:
                            print(f"⚠️ 取 {sym} open orders 失敗（重試）：{e}")
                        time.sleep(PER_SYMBOL_SLEEP_SEC)
                finally:
                    time.sleep(PER_SYMBOL_SLEEP_SEC)  # 節流
        return results

    # ---- 快速路徑（舊：低階一次撈 / 逐 symbol 低階）----
    try:
        ods = _fapi_signed_get('/fapi/v1/allOpenOrders', {})
        if isinstance(ods, list):
            return ods
    except Exception as e:
        if RECONCILE_VERBOSE:
            print(f"⚠️ 低階 allOpenOrders 失敗（將改走逐 symbol）：{e}")

    results = []
    pos_syms = set(s for (s, _side) in _get_open_positions_set())
    symbols_to_check = list(pos_syms) if pos_syms else _list_all_active_symbols()
    for sym in symbols_to_check:
        try:
            fetched = _fapi_signed_get('/fapi/v1/openOrders', {'symbol': sym})
            if isinstance(fetched, list) and fetched:
                results.extend(fetched)
        except Exception as e:
            if RECONCILE_VERBOSE:
                print(f"⚠️ 低階取 {sym} openOrders 失敗：{e}")
            continue
    return results

def resume_trades_from_state(event_loop=None):
    """
    程式重啟後，根據 chao_bi_state.json 嘗試恢復：
    1) 還掛著但未完全成交的 LIMIT 開倉單 → 重新啟動 monitor_and_auto_cancel
    2) 已完全成交但缺少 SL/TP 的倉位 → 依當初紀錄的 SL/TP 補掛風控單
    3) 已被撤單 / 查無此單 → 自狀態檔移除
    """
    if binance_client is None:
        print("⚠️ 無法恢復狀態：幣安客戶端未初始化。")
        return

    if not _tracked_trades:
        print("ℹ️ 沒有需要恢復的交易狀態。")
        return

    print(f"🔁 嘗試恢復 {len(_tracked_trades)} 筆已記錄交易狀態 …")
    for key, rec in list(_tracked_trades.items()):
        try:
            entry_id = rec.get("entry_order_id") or int(key)
            symbol = rec.get("symbol")
            position_side = (rec.get("position_side") or "LONG").upper()
            sl_price = rec.get("stop_loss")
            tp_price = rec.get("take_profit")

            if not symbol or not entry_id:
                clear_closed_trade(key)
                continue

            od = _query_order(symbol, order_id=int(entry_id))
            if not od:
                # 查無此單，視為已結束
                clear_closed_trade(entry_id)
                continue

            status = str(od.get("status", "")).upper()
            otype = str(od.get("type", "")).upper()

            # 若已被取消/過期/拒絕，直接清掉
            if status in ("CANCELED", "EXPIRED", "REJECTED"):
                clear_closed_trade(entry_id)
                continue

            # case 1: LIMIT 單還在 NEW/PARTIALLY_FILLED → 恢復長時間監控（避免重複啟動）
            if otype == "LIMIT" and status in ("NEW", "PARTIALLY_FILLED"):
                key_tuple = (symbol, int(entry_id))
                if key_tuple in _monitoring_orders:
                    print(f"ℹ️ 開倉單 {entry_id} ({symbol}) 已在監控中，略過重複註冊。")
                elif event_loop is not None and getattr(event_loop, "is_running", lambda: False)():
                    try:
                        _monitoring_orders.add(key_tuple)
                        asyncio.run_coroutine_threadsafe(
                            monitor_and_auto_cancel(
                                symbol,
                                int(entry_id),
                                position_side,
                                str(sl_price),
                                str(tp_price),
                                AUTO_CANCEL_SECONDS,
                                ORDER_MONITOR_INTERVAL,
                            ),
                            event_loop,
                        )
                        print(f"⏱️ 已恢復監控開倉單 {entry_id} ({symbol})。")
                    except Exception as e:
                        _monitoring_orders.discard(key_tuple)
                        print(f"⚠️ 恢復監控 {symbol}/{entry_id} 失敗：{e}")
                else:
                    print(f"⚠️ 事件迴圈不可用，無法恢復監控 {symbol}/{entry_id}。")
                continue

            # case 2: 已經 FILLED/部分成交且有持倉，但可能缺少 SL/TP → 補掛
            if status in ("FILLED", "PARTIALLY_FILLED"):
                pos_amt = _get_position_amount(symbol, position_side)
                if pos_amt == 0:
                    # 沒有倉位了，清掉紀錄
                    clear_closed_trade(entry_id)
                    continue

                # 檢查是否已存在 closePosition/reduceOnly 的 SL/TP 單
                try:
                    open_ods = _sdk_get_open_orders(symbol)
                except Exception as e:
                    print(f"⚠️ 讀取 {symbol} open orders 失敗，略過 SL/TP 檢查：{e}")
                    continue

                has_exit = False
                for od2 in open_ods or []:
                    try:
                        ps = (od2.get("positionSide") or "").upper()
                        if ps != position_side:
                            continue
                        otype2 = str(od2.get("type", "")).upper()
                        close_str = str(od2.get("closePosition", od2.get("closeposition", ""))).lower()
                        is_close_position = (close_str == "true")
                        is_reduce_only = str(od2.get("reduceOnly", "")).lower() == "true"
                        is_exit_type = otype2 in ("STOP", "STOP_MARKET", "TAKE_PROFIT", "TAKE_PROFIT_MARKET")
                        if is_exit_type and (is_close_position or is_reduce_only):
                            has_exit = True
                            break
                    except Exception:
                        continue

                if has_exit:
                    print(f"ℹ️ {symbol}/{entry_id} 已存在 SL/TP 關倉單，略過補掛。")
                    continue

                if sl_price is None or tp_price is None:
                    print(f"⚠️ {symbol}/{entry_id} 沒有完整 SL/TP 記錄，無法自動補掛。")
                    continue

                try:
                    sl_id, tp_id = _attach_exits_after_fill(
                        symbol,
                        position_side,
                        str(sl_price),
                        str(tp_price),
                        entry_order_id=entry_id,
                    )
                    print(f"🔁 已替 {symbol}/{entry_id} 補掛 SL/TP。")
                except Exception as e:
                    print(f"⚠️ 補掛 {symbol}/{entry_id} SL/TP 失敗：{e}")
                continue

        except Exception as e:
            print(f"⚠️ 恢復狀態時處理 {key} 發生錯誤：{e}")
    print("🔁 狀態恢復檢查完成。")

def reconcile_on_start(event_loop=None, timeout_seconds=AUTO_CANCEL_SECONDS):
    """
    啟動時自動清理『舊的未成交開倉單』與『孤兒 SL/TP 關倉單』：
    1) 任何非 closePosition 的開倉單（LIMIT 或仍 open 的 MARKET），若下單超過 timeout_seconds 未成交 → 撤單
    2) 任何 closePosition 的 SL/TP 關倉單，若對應倉位不存在（已平倉）→ 撤單
    """
    print("[Reconcile] 啟動自動清理程序 …")
    summary = {"stale_entries": [], "orphan_exits": []}
    try:
        open_orders = _get_all_open_orders()  # 匯總所有 symbol 的 open 訂單
        try:
            print(f"[Reconcile] 掃描完成，open orders 彙整筆數：{len(open_orders)}")
        except Exception:
            pass
    except Exception as e:
        print(f"[error] 讀取開放訂單未知錯誤：{e}")
        return summary

    now_ms = int(time.time() * 1000)
    pos_set = _get_open_positions_set()
    if RECONCILE_VERBOSE:
        print(f"[ReconcileVerbose] current non-zero positions: {sorted(list(pos_set))}")

    for od in open_orders:
        try:
            if RECONCILE_VERBOSE:
                try:
                    print(f"[ReconcileVerbose] raw open order: {json.dumps(od, ensure_ascii=False)}")
                except Exception:
                    print(f"[ReconcileVerbose] raw open order (repr): {od}")
            symbol = od.get('symbol')
            order_id = od.get('orderId')
            otype = od.get('type')
            pos_side = od.get('positionSide') or ('LONG' if od.get('side') == 'BUY' else 'SHORT')

            # --- Normalize exit flags ---
            # Some TP/SL orders may return only reduceOnly=True (without closePosition)
            close_str = str(od.get('closePosition', od.get('closeposition', ''))).lower()
            is_close_position = (close_str == 'true')
            is_reduce_only = str(od.get('reduceOnly', '')).lower() == 'true'
            otype = (od.get('type') or '').upper()
            is_exit_type = otype in ('STOP_MARKET', 'TAKE_PROFIT_MARKET', 'STOP', 'TAKE_PROFIT')
            consider_exit = is_close_position or (is_reduce_only and is_exit_type)

            # Derive create_time
            create_time = int(od.get('time', od.get('updateTime', 0)))

            # Derive position side if missing
            if not pos_side:
                # For exits, BUY closes SHORT; SELL closes LONG
                if consider_exit:
                    pos_side = 'SHORT' if str(od.get('side', '')).upper() == 'BUY' else 'LONG'
                else:
                    pos_side = 'LONG' if str(od.get('side', '')).upper() == 'BUY' else 'SHORT'

            # (A) Orphan exits: exit order exists but there is no corresponding position
            if consider_exit:
                # 先查精確倉位數量
                position_amt = _get_position_amount(symbol, pos_side)
                if RECONCILE_VERBOSE:
                    print(f"[ReconcileVerbose] positionAmt({symbol}, {pos_side}) = {position_amt}")
                amt_abs = abs(position_amt)
                if amt_abs == Decimal('0'):
                    # 無部位，視為孤兒單
                    ok = _cancel_order_safely(symbol, order_id)
                    if ok:
                        summary["orphan_exits"].append({"symbol": symbol, "orderId": order_id, "type": otype, "positionSide": pos_side})
                        notify_user(
                            text=(f"🧹 清理：孤兒 SL/TP 已撤單\n"
                                  f"• 標的: {symbol}\n"
                                  f"• 類型: {otype}\n"
                                  f"• 方向: {pos_side}\n"
                                  f"• OrderID: {order_id}"),
                            loop=event_loop
                        )

                        # --- LOGGING ---
                        entry_order_id = None
                        for key, trade in _tracked_trades.items():
                            if trade.get('sl_order_id') == order_id or trade.get('tp_order_id') == order_id:
                                entry_order_id = key
                                break
                        
                        if entry_order_id and entry_order_id in _tracked_trades:
                            trade_to_log = _tracked_trades[entry_order_id]
                            try:
                                user_trades = binance_client.user_trades(symbol=symbol, limit=10)
                                for user_trade in reversed(user_trades):
                                    if user_trade['orderId'] == order_id:
                                        pnl = float(user_trade.get('realizedPnl', 0.0))
                                        win_loss_draw = 'WIN' if pnl > 0 else 'LOSS' if pnl < 0 else 'DRAW'
                                        log_trade({
                                            'symbol': symbol,
                                            'position_side': pos_side,
                                            'entry_price': trade_to_log.get('entry_price'),
                                            'exit_price': user_trade.get('price'),
                                            'quantity': trade_to_log.get('quantity'),
                                            'leverage': trade_to_log.get('leverage'),
                                            'pnl': pnl,
                                            'signal_source': trade_to_log.get('channel_title'),
                                            'win_loss_draw': win_loss_draw,
                                            'raw_signal': trade_to_log.get('raw_signal')
                                        })
                                        clear_closed_trade(entry_order_id)
                                        break
                            except Exception as e:
                                print(f"[error] Failed to log trade {entry_order_id}: {e}")

                    continue
                # 備用: 若仍不在 pos_set，亦撤單 (防不一致)
                if (symbol, pos_side) not in pos_set:
                    ok = _cancel_order_safely(symbol, order_id)
                    if ok:
                        summary["orphan_exits"].append({"symbol": symbol, "orderId": order_id, "type": otype, "positionSide": pos_side})
                        notify_user(
                            text=(f"🧹 清理：孤兒 SL/TP 已撤單\n"
                                  f"• 標的: {symbol}\n"
                                  f"• 類型: {otype}\n"
                                  f"• 方向: {pos_side}\n"
                                  f"• OrderID: {order_id}"),
                            loop=event_loop
                        )
                    continue
                continue

            # (B) 陳舊開倉單：非 closePosition，超過逾時未完全成交
            if create_time and (now_ms - create_time) >= (timeout_seconds * 1000):
                ok = _cancel_order_safely(symbol, order_id)
                if ok:
                    summary["stale_entries"].append({"symbol": symbol, "orderId": order_id, "type": otype, "positionSide": pos_side})
                    try:
                        clear_closed_trade(order_id)
                    except Exception as e:
                        print(f"[error] Reconcile 移除本地狀態失敗：{e}")
                    notify_user(
                        text=(f"🕒 清理：逾時未成交的開倉單已撤\n"
                              f"• 標的: {symbol}\n"
                              f"• 類型: {otype}\n"
                              f"• 方向: {pos_side}\n"
                              f"• OrderID: {order_id}"),
                        loop=event_loop
                    )
        except Exception as e:
            print(f"⚠️ 清理該筆訂單時發生錯誤：{e}")

    print(f"🔧 [Reconcile] 完成。撤掉 {len(summary['stale_entries'])} 筆舊開倉、{len(summary['orphan_exits'])} 筆孤兒關倉。")
    # 只有在有實際撤單動作時才通知，避免零動作打擾
    if (len(summary["stale_entries"]) + len(summary["orphan_exits"])) > 0:
        try:
            notify_user(
                text=(f"🔧 啟動清理完成\n"
                      f"• 舊開倉撤單: {len(summary['stale_entries'])}\n"
                      f"• 孤兒 SL/TP 撤單: {len(summary['orphan_exits'])}"),
                loop=event_loop
            )
        except Exception:
            pass
    return summary

async def monitor_and_auto_cancel(symbol, order_id, position_side, sl_price_str, tp_price_str, timeout_seconds=AUTO_CANCEL_SECONDS, poll_interval=ORDER_MONITOR_INTERVAL):
    """ 
    監控未成交的『開倉 LIMIT 單』；超過 timeout 仍未完全成交則自動撤單。
    偵測到成交時立刻補掛 SL/TP。
    """
    key_tuple = (symbol, order_id)
    print(f"   [Monitor] 開始監控 {symbol} 訂單 {order_id}，逾時 {timeout_seconds}s 未成交將撤單。")
    exits_attached = False
    t0 = time.time()
    try:
        while True:
            await asyncio.sleep(poll_interval)
            try:
                q = _query_order(symbol, order_id=order_id)
                if not q:
                    continue
                status = str(q.get('status', ''))
                if status in ('PARTIALLY_FILLED', 'FILLED'):
                    if not exits_attached:
                        try:
                            sl_id, tp_id = _attach_exits_after_fill(
                                symbol,
                                position_side,
                                sl_price_str,
                                tp_price_str,
                                entry_order_id=order_id
                            )
                            exits_attached = True
                            print(f"   [Monitor] 偵測到成交（{status}），已立刻補掛 SL/TP。")
                            notify_user(
                                text=(f"📎 監控：補掛 SL/TP\n"
                                      f"• 標的: {symbol}\n"
                                      f"• 狀態: {status}\n"
                                      f"• SL: {sl_price_str} (ID: {sl_id})\n"
                                      f"• TP: {tp_price_str} (ID: {tp_id})\n"
                                      f"• OrderID: {order_id}"),
                                loop=client.loop if client else None
                            )
                        except Exception as ee:
                            print(f"   [Monitor] 補掛 SL/TP 失敗：{ee}")
                    if status == 'FILLED':
                        print(f"   [Monitor] 訂單 {order_id} 已完全成交，停止監控。")
                        # 通知完全成交
                        notify_user(
                            text=(f"✅ 監控：開倉單已完全成交\n"
                                  f"• 標的: {symbol}\n"
                                  f"• OrderID: {order_id}"),
                            loop=client.loop if client else None
                        )
                        return
                    # PARTIALLY_FILLED: 繼續等，直到完全成交或逾時
                elif status in ('CANCELED', 'EXPIRED', 'REJECTED'):
                    print(f"   [Monitor] 訂單 {order_id} 狀態 {status}，停止監控。")
                    try:
                        clear_closed_trade(order_id)
                    except Exception as e:
                        print(f"⚠️ 移除本地狀態失敗：{e}")
                    break
                if time.time() - t0 >= timeout_seconds:
                    if status != 'FILLED':
                        print(f"   [Monitor] 超過 {timeout_seconds}s 未完全成交，嘗試撤單 {order_id} ...")
                        try:
                            binance_client.cancel_order(symbol=symbol, orderId=order_id)
                            print(f"   ✅ 已撤單 {order_id}（若部分成交，僅撤未成交殘量）。")
                            try:
                                clear_closed_trade(order_id)
                                # 通知超時撤單
                                notify_user(
                                    text=(f"🕒 監控：超過期限未完全成交，已撤單\n"
                                        f"• 標的: {symbol}\n"
                                        f"• OrderID: {order_id}"),
                                    loop=client.loop if client else None
                                )
                            except Exception as e:
                                print(f"⚠️ 移除本地狀態失敗：{e}")
                                # 通知超時撤單
                                notify_user(
                                    text=(f"🕒 監控：超過期限未完全成交，已撤單，移除本地狀態失敗\n"
                                        f"• 標的: {symbol}\n"
                                        f"• OrderID: {order_id}"),
                                    loop=client.loop if client else None
                                )
                        except ClientError as e:
                            print(f"   ❌ 撤單失敗：{e}")
                            # 通知撤單失敗
                            notify_user(
                                text=(f"⚠️ 監控：撤單失敗\n"
                                      f"• 標的: {symbol}\n"
                                      f"• OrderID: {order_id}\n"
                                      f"• 錯誤: {e}"),
                                loop=client.loop if client else None
                            )
                    return
            except Exception as e:
                print(f"   [Monitor] 查詢訂單時發生錯誤：{e}")
    finally:
        # 無論正常結束或遇到例外，皆移除監控標記
        try:
            _monitoring_orders.discard(key_tuple)
        except Exception:
            pass

def _attach_exits_after_fill(symbol, position_side, sl_price_str, tp_price_str,
                             working_type='MARK_PRICE', entry_order_id=None):
    """
    在『倉位已建立』後，送出 SL/TP 兩張【條件關倉單】。
    使用 STOP_MARKET / TAKE_PROFIT_MARKET + closePosition="true"。
    """
    close_side = 'SELL' if position_side == 'LONG' else 'BUY'

    sl_order_params = {
        'symbol': symbol,
        'side': close_side,
        'positionSide': position_side,
        'type': 'STOP_MARKET',
        'stopPrice': sl_price_str,
        'closePosition': "true",
        'workingType': working_type,
        'priceProtect': "true",
    }
    tp_order_params = {
        'symbol': symbol,
        'side': close_side,
        'positionSide': position_side,
        'type': 'TAKE_PROFIT_MARKET',
        'stopPrice': tp_price_str,
        'closePosition': "true",
        'workingType': working_type,
        'priceProtect': "true",
    }

    try:
        print("   [Binance] 成交後掛上止損單 (STOP_MARKET, closePosition=true)...")
        res1 = binance_client.new_order(**sl_order_params)
        sl_id = res1.get('orderId')
        print(f"   ✅ SL 已掛上 (ID: {sl_id})")

        print("   [Binance] 成交後掛上止盈單 (TAKE_PROFIT_MARKET, closePosition=true)...")
        res2 = binance_client.new_order(**tp_order_params)
        tp_id = res2.get('orderId')
        print(f"   ✅ TP 已掛上 (ID: {tp_id})")
        try:
            if entry_order_id is not None:
                update_exits_for_trade(entry_order_id, sl_id, tp_id)
        except Exception as e:
            print(f"⚠️ 更新本地狀態 SL/TP 失敗：{e}")
        return sl_id, tp_id
    except ClientError as e:
        print(f"❌ 成交後掛 SL/TP 失敗：{e}")
        return None, None