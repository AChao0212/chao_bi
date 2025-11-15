import asyncio 
import time
from decimal import Decimal, ROUND_DOWN, ROUND_UP
from config import (
    RISK_PER_TRADE_PERCENT,MAX_INITIAL_MARGIN_PCT,
    POSITION_SIZING_MODE,USE_PY_RISK_MANAGER,
    AUTO_CANCEL_SECONDS, ORDER_MONITOR_INTERVAL,
    INITIAL_FILL_WAIT_SECONDS, INITIAL_POLL_INTERVAL,
)
from state_store import (
    register_entry_trade, load_state
)
from llm import (
    parse_signal_with_llm,
    complete_trade_with_llm,
)
from telegram import (
    client, notify_user
)
from binance_api import (
    binance_client, get_symbol_info,
    set_binance_leverage, format_value_by_precision,
    get_binance_market_price, _get_lot_size_filter,
    total_available_margin, _cap_qty_by_initial_margin,
    _query_order, monitor_and_auto_cancel,
    _attach_exits_after_fill, normalize_aliases,
    is_valid_symbol, get_binance_klines_for_llm,
    apply_leverage_override, select_sl_tp_with_user_pref,
    sanitize_targets, reconcile_on_start,
    daily_pnl_notifier, resume_trades_from_state,
)
# --- [warning] 導入幣安官方 SDK (v32) [warning] ---
try:
    from binance.error import ClientError
except ImportError as e:
    print(f"[error] 致命錯誤：找不到 'binance.um_futures' 模組！")
    print(f"錯誤詳情: {e}")
    exit()

# --- 導入 Telethon (v32) ---
try:
    from telethon import events
except ImportError as e:
    print(f"[error] 致命錯誤：找不到 'telethon' 模組！")
    print(f"錯誤詳情: {e}")
    exit()

# === [executor] 真實下單主流程 ===
def execute_trade(trade_command: dict, event_loop=None):
    """真實下單流程：先下【開倉單】，成交後再掛【SL/TP 關倉單】。"""
    if binance_client is None:
        print("[error] 交易失敗：幣安客戶端未初始化。")
        return

    print("\n" + "="*30)
    print(f"🚨🚨🚨 執行交易 (!!! 真實環境 !!!) 🚨🚨🚨")
    print(f"   動作: {trade_command.get('action')}")
    print(f"   標的: {trade_command.get('symbol')}")
    print(f"   入場: {trade_command.get('entry_price')}")
    print(f"   止盈: {trade_command.get('take_profit')}")
    print(f"   止損: {trade_command.get('stop_loss')}")
    print(f"   槓桿: {trade_command.get('leverage')}x")
    print(f"   數量: {trade_command.get('quantity')}")
    print("="*30)

    symbol = trade_command.get('symbol')
    action = trade_command.get('action')  # 'BUY' or 'SELL'
    is_buy_signal = action.upper() == "BUY"
    entry_price = trade_command.get('entry_price')  # 可能是 None (市價)
    leverage = trade_command.get('leverage')
    stop_loss_price = trade_command.get('stop_loss')
    take_profit_price = trade_command.get('take_profit')
    quantity = trade_command.get('quantity')
    signal_text = trade_command.get('signal_text') or ''

    # 1) 設定槓桿（並取得實際生效倍數）
    requested_leverage = None if (trade_command.get('leverage') is None) else int(trade_command.get('leverage'))
    if requested_leverage is None:
        print(f"[error] 交易失敗：LLM 未能提供槓桿，已取消下單。")
        return
    applied_leverage = set_binance_leverage(symbol, requested_leverage)
    if not applied_leverage:
        print(f"[error] 交易失敗：設定 {requested_leverage}x 槓桿失敗，已取消下單。")
        return
    # 用『實際生效的倍數』覆寫本地變數與 trade_command，之後所有計算都以此為準
    leverage = int(applied_leverage)
    trade_command['leverage'] = leverage
    if requested_leverage != leverage:
        print(f"   [Binance] 槓桿已自動回退至 {leverage}x（原請求 {requested_leverage}x）。")

    # 2) 交易對精度
    print(f"   [Binance] 正在獲取 {symbol} 交易對資訊...")
    info = get_symbol_info(symbol)
    if not info:
        print(f"[error] 交易失敗：無法獲取 {symbol} 資訊，已停止下單")
        return

    try:
        price_precision = next(f['tickSize'] for f in info['filters'] if f['filterType'] == 'PRICE_FILTER')
        lot_filter = _get_lot_size_filter(info)
        if not lot_filter:
            raise RuntimeError("找不到 LOT_SIZE 濾器")
        quantity_precision = lot_filter.get('stepSize')
        min_qty_str = lot_filter.get('minQty', '0')
        sl_tp_precision = price_precision

        # 先處理數量：避免被 stepSize 四捨五入到 0
        dec_qty = Decimal(str(quantity))
        step_dec = Decimal(str(quantity_precision))
        min_qty_dec = Decimal(str(min_qty_str))

        # 若 entry_price 是 None（市價），用即時市價做參考
        ref_price_dec = None
        try:
            if entry_price is None:
                mp = get_binance_market_price(symbol)
                if mp:
                    ref_price_dec = Decimal(str(mp))
            else:
                ref_price_dec = Decimal(str(entry_price))
        except Exception:
            ref_price_dec = None

        # 依保證金上限推導可承受的最大數量；若 ref_price_dec 缺失則跳過此保護
        cap_qty_by_margin = None
        try:
            if ref_price_dec and Decimal(str(leverage)) > 0:
                cap_qty_by_margin = (Decimal(str(total_available_margin)) * Decimal(str(MAX_INITIAL_MARGIN_PCT)) * Decimal(str(leverage))) / ref_price_dec
        except Exception:
            cap_qty_by_margin = None

        # 若計算結果低於交易所最低數量，嘗試 bump 至 minQty（但不得超過保證金上限）
        if dec_qty < min_qty_dec:
            if cap_qty_by_margin is not None and min_qty_dec > cap_qty_by_margin:
                raise RuntimeError(f"計算數量 {dec_qty} < 交易所最小數量 {min_qty_dec}，且超出保證金上限，取消下單以避免風險擴大")
            print(f"   [Binance 提示] 數量 {dec_qty} 低於最小下單量 {min_qty_dec}，自動提升至最小量")
            dec_qty = min_qty_dec

        # 以 stepSize 對齊：先向下取整；若變成 0，則改用向上取整到一個 step
        formatted_quantity = format_value_by_precision(dec_qty, quantity_precision, ROUND_DOWN)
        if Decimal(formatted_quantity) == 0:
            # 向上取一個 step
            bumped = ( (dec_qty // step_dec) * step_dec )
            if bumped < dec_qty:
                bumped = bumped + step_dec
            if cap_qty_by_margin is not None and bumped > cap_qty_by_margin:
                raise RuntimeError(f"向上取整後的數量 {bumped} 超出保證金上限，取消下單")
            formatted_quantity = format_value_by_precision(str(bumped), quantity_precision, ROUND_UP)

        # 保底：若仍為 0，直接以最小 step 下單（若允許）
        if Decimal(formatted_quantity) == 0:
            if cap_qty_by_margin is not None and step_dec > cap_qty_by_margin:
                raise RuntimeError("最小 step 高於保證金上限，取消下單")
            formatted_quantity = format_value_by_precision(str(step_dec), quantity_precision, ROUND_UP)

        # —— 追加：滿足 MIN_NOTIONAL（期貨） ——
        try:
            min_notional_filter = next((f for f in info['filters'] if f.get('filterType') == 'MIN_NOTIONAL'), None)
            if min_notional_filter and ref_price_dec is not None:
                min_notional_dec = Decimal(str(min_notional_filter.get('notional', '0')))
                # 以目前 formatted_quantity 檢查名義金額是否不足
                cur_qty_dec = Decimal(str(formatted_quantity))
                cur_notional = (ref_price_dec * cur_qty_dec)
                if cur_notional < min_notional_dec:
                    # 計算達標所需最小數量，並以 stepSize 向上取整
                    required_qty = (min_notional_dec / ref_price_dec)
                    # 以 step 對齊向上進位：ceil(required/step)*step
                    steps_needed = (required_qty / step_dec).to_integral_value(rounding=ROUND_UP)
                    bumped_qty = steps_needed * step_dec
                    # 檢查保證金上限
                    if cap_qty_by_margin is not None and bumped_qty > cap_qty_by_margin:
                        raise RuntimeError(
                            f"名義金額不足（{cur_notional} < {min_notional_dec}），而達標所需數量 {bumped_qty} 超過保證金上限，取消下單")
                    formatted_quantity = format_value_by_precision(str(bumped_qty), quantity_precision, ROUND_UP)
        except Exception as e_min_notional:
            print(f"[warning] MIN_NOTIONAL 檢查/調整失敗：{e_min_notional}")

        # 若為市價單且 ref_price_dec 仍為 None，補查市價
        if ref_price_dec is None:
            try:
                mp2 = get_binance_market_price(symbol)
                if mp2:
                    ref_price_dec = Decimal(str(mp2))
            except Exception:
                pass

        # —— 最終硬封頂：再次以初始保證金 3% 做上限 ——
        try:
            lev_dec2 = Decimal(str(leverage))
            if ref_price_dec is not None and lev_dec2 > 0:
                max_margin_amt2 = Decimal(str(total_available_margin)) * Decimal(str(MAX_INITIAL_MARGIN_PCT))
                cur_qty_dec2 = Decimal(str(formatted_quantity))
                capped_qty = _cap_qty_by_initial_margin(ref_price_dec, lev_dec2, cur_qty_dec2,
                                                        max_margin_amt2, Decimal(str(quantity_precision)), Decimal(str(min_qty_str)))
                if capped_qty == Decimal('0'):
                    print("[error] 交易取消：在最終封頂後，最小下單量也超出 3% 保證金上限。")
                    notify_user(
                        text=(f"[warning] 已取消下單（超出 3% 初始保證金上限）\n"
                              f"• 標的: {symbol}\n"
                              f"• 計算後數量無法符合上限與最小下單量"),
                        loop=event_loop
                    )
                    return
                formatted_quantity = format_value_by_precision(str(capped_qty), quantity_precision, ROUND_DOWN)
        except Exception as e_cap:
            print(f"[warning] 初始保證金封頂檢查失敗：{e_cap}")

        # 格式化價格
        formatted_price = None
        if entry_price is not None:
            round_mode = ROUND_DOWN if is_buy_signal else ROUND_UP
            formatted_price = format_value_by_precision(entry_price, price_precision, round_mode)

        sl_round_mode = ROUND_UP if is_buy_signal else ROUND_DOWN
        tp_round_mode = ROUND_DOWN if is_buy_signal else ROUND_UP
        formatted_sl_price = format_value_by_precision(stop_loss_price, sl_tp_precision, sl_round_mode)
        formatted_tp_price = format_value_by_precision(take_profit_price, sl_tp_precision, tp_round_mode)

        if formatted_price:
            print(f"   [Binance] 價格格式化為 {formatted_price}")
        else:
            print(f"   [Binance] 價格為 市價 (MARKET)")
        print(f"   [Binance] 數量格式化為 {formatted_quantity}")
        print(f"   [Binance] 止損價格式化為 {formatted_sl_price}")
        print(f"   [Binance] 止盈價格式化為 {formatted_tp_price}")
        # 額外 log：名義金額與最小門檻
        try:
            min_notional_filter = next((f for f in info['filters'] if f.get('filterType') == 'MIN_NOTIONAL'), None)
            if min_notional_filter and ref_price_dec is not None:
                min_notional_dec = Decimal(str(min_notional_filter.get('notional', '0')))
                cur_notional = (ref_price_dec * Decimal(str(formatted_quantity)))
                print(f"   [Binance] 名義金額 ≈ {cur_notional}（最小門檻 {min_notional_dec}）")
        except Exception:
            pass

    except Exception as e:
        print(f"[error] 交易失敗：格式化精度時出錯: {e}")
        return

    # 若最終數量仍為 0，直接中止，避免 -4003
    if Decimal(str(formatted_quantity)) == 0:
        print("[error] 交易失敗：數量在精度對齊後仍為 0，已取消下單。")
        return

    # 最後檢查：以 ref_price_dec 預估初始保證金比例，不得超過 3%
    try:
        if ref_price_dec is not None and Decimal(str(leverage)) > 0:
            est_initial_margin = (ref_price_dec * Decimal(str(formatted_quantity))) / Decimal(str(leverage))
            cap_amt = Decimal(str(total_available_margin)) * Decimal(str(MAX_INITIAL_MARGIN_PCT))
            if est_initial_margin > cap_amt * Decimal('1.001'):
                print(f"[error] 交易取消：估算初始保證金 {est_initial_margin} 超過上限 {cap_amt}")
                notify_user(
                    text=(f"[warning] 已取消下單（初始保證金超標）\n"
                          f"• 標的: {symbol}\n"
                          f"• 估算初始保證金: {est_initial_margin}\n"
                          f"• 上限(3%): {cap_amt}"),
                    loop=event_loop
                )
                return
    except Exception as e_chk:
        print(f"[warning] 初始保證金最終檢查失敗（將繼續）：{e_chk}")

    # 3) 先送開倉單（單筆），回傳 orderId / clientOrderId
    position_side = "LONG" if is_buy_signal else "SHORT"
    order_type = 'LIMIT' if formatted_price else 'MARKET'
    entry_order_params = {
        'symbol': symbol,
        'side': action,
        'positionSide': position_side,
        'type': order_type,
        'quantity': formatted_quantity,
        'newOrderRespType': 'RESULT',  # 盡可能拿到即時結果
    }
    if order_type == 'LIMIT':
        entry_order_params['price'] = formatted_price
        entry_order_params['timeInForce'] = 'GTC'

    try:
        print("   [Binance 動作] 送出『開倉單』 ...")
        entry_resp = binance_client.new_order(**entry_order_params)
        print(f"   ✅ 開倉單已送出。狀態: {entry_resp.get('status')}，ID: {entry_resp.get('orderId')}")
        order_id = entry_resp.get('orderId')
        try:
            register_entry_trade(
                symbol=symbol,
                position_side=position_side,
                order_type=order_type,
                entry_price=(formatted_price or (str(ref_price_dec) if ref_price_dec is not None else None)),
                quantity=formatted_quantity,
                leverage=leverage,
                stop_loss=formatted_sl_price,
                take_profit=formatted_tp_price,
                entry_order_id=order_id,
            )
        except Exception as e:
            print(f"[warning] 記錄開倉單狀態失敗（不影響下單）：{e}")
        try:
            decision_signal = ("市價觸發" if order_type == 'MARKET' else f"限價@{formatted_price}") + " | 解析: " + (signal_text[:80] if signal_text else "N/A")
            notify_user(
                text=(f"📤 已送出開倉單\n"
                    f"• 標的: {symbol}\n"
                    f"• 方向: {action} ({position_side})\n"
                    f"• 類型: {order_type}\n"
                    f"• 價格: {formatted_price or 'MARKET'}\n"
                    f"• 數量: {formatted_quantity}\n"
                    f"• 槓桿: {leverage}x\n"
                    f"• 決策訊號: {decision_signal}\n"
                    f"• 初始保證金(估): {((Decimal(str(formatted_price or ref_price_dec or '0')) * Decimal(str(formatted_quantity))) / Decimal(str(leverage)) if (formatted_quantity and leverage and (formatted_price or ref_price_dec)) else 'N/A')}\n"
                    f"• OrderID: {entry_resp.get('orderId')}\n"
                    + (f"• 槓桿回退: {requested_leverage}x → {leverage}x\n" if requested_leverage != leverage else "")
                    + f"• 來源訊號: {signal_text}"),
            )
        except Exception:
            pass
    except ClientError as e:
        print(f"[error] 開倉下單失敗：{e}")
        print("="*30 + "\n")
        return

    # 4) 等待成交（或 MARKET 視為立即成交），成交後再掛 SL/TP
    order_id = entry_resp.get('orderId')
    filled = False

    if order_type == 'MARKET':
        # MARKET 一般直接 FILLED
        filled = True
        print("   [Binance] 市價單視為已成交。")
        try:
            decision_signal = ("市價觸發" if order_type == 'MARKET' else f"限價@{formatted_price}") + " | 解析: " + (signal_text[:80] if signal_text else "N/A")
            notify_user(
                text=(
                    f"✅ 市價單已成交\n"
                    f"• 標的: {symbol}\n"
                    f"• 方向: {action} ({position_side})\n"
                    f"• 決策訊號: {decision_signal}\n"
                    f"• 將掛 SL/TP: SL {formatted_sl_price} / TP {formatted_tp_price}\n"
                    f"• OrderID: {order_id}\n"
                    + (f"• 槓桿回退: {requested_leverage}x → {leverage}x\n" if requested_leverage != leverage else "")
                    + f"• 來源訊號: {signal_text}"
                ),
                loop=event_loop
            )
        except Exception:
            pass
    else:
        # LIMIT：輪詢查詢訂單狀態
        print(f"   [Binance] 等待開倉單成交 (最多 {INITIAL_FILL_WAIT_SECONDS} 秒，每 {INITIAL_POLL_INTERVAL} 秒檢查一次)...")
        t0 = time.time()
        while time.time() - t0 < INITIAL_FILL_WAIT_SECONDS:
            time.sleep(INITIAL_POLL_INTERVAL)
            q = _query_order(symbol, order_id=order_id)
            if not q:
                continue
            status = str(q.get('status', ''))
            if status in ('FILLED', 'PARTIALLY_FILLED'):
                filled = True
                print(f"   [Binance] 開倉單狀態: {status}，已準備掛 TP/SL。")
                try:
                    decision_signal = ("市價觸發" if order_type == 'MARKET' else f"限價@{formatted_price}") + " | 解析: " + (signal_text[:80] if signal_text else "N/A")
                    notify_user(
                        text=(f"✅ 開倉單成交狀態: {status}\n"
                            f"• 標的: {symbol}\n"
                            f"• 方向: {action} ({position_side})\n"
                            f"• 決策訊號: {decision_signal}\n"
                            f"• 將掛 SL/TP: SL {formatted_sl_price} / TP {formatted_tp_price}\n"
                            f"• OrderID: {order_id}\n"
                            + (f"• 槓桿回退: {requested_leverage}x → {leverage}x\n" if requested_leverage != leverage else "")
                            + f"• 來源訊號: {signal_text}"),
                        loop=event_loop
                    )
                except Exception:
                    pass
                break
            elif status in ('CANCELED', 'EXPIRED', 'REJECTED'):
                print(f"[error] 開倉單未成交（狀態: {status}），取消掛 TP/SL。")
                break

    if not filled:
        # 未在短時間內成交：啟動長時監控，逾時自動撤單（在主事件迴圈中排程）
        try:
            if event_loop and hasattr(event_loop, "is_running") and event_loop.is_running():
                asyncio.run_coroutine_threadsafe(
                    monitor_and_auto_cancel(symbol, order_id, position_side, formatted_sl_price, formatted_tp_price, AUTO_CANCEL_SECONDS, ORDER_MONITOR_INTERVAL),
                    event_loop
                )
                print(f"   [Binance] 已啟動 12 小時未成交自動撤單監控（訂單 {order_id}）。")
            else:
                print("   [warning] 無法啟動監控任務：主事件迴圈不可用，略過背景監控。")
        except Exception as e:
            print(f"   [warning] 無法啟動監控任務：{e}")
        print("="*30 + "\n")
        return

    # 5) 安全檢查：避免「立即觸發」的 TP（可依偏好關掉）
    try:
        current_mark_price_str = get_binance_market_price(symbol)
        current_mark_price = Decimal(current_mark_price_str)
        tp_price_dec = Decimal(formatted_tp_price)

        print(f"   [Binance] 成交後 TP 檢查：目標 {tp_price_dec}，當前 {current_mark_price}")
        will_trigger_immediately = (is_buy_signal and tp_price_dec <= current_mark_price) or \
                                   ((not is_buy_signal) and tp_price_dec >= current_mark_price)
        if will_trigger_immediately:
            print("[warning] [Binance 提示] TP 將立即觸發。依照目前設定，為避免『成交即平倉』，**略過** TP（仍保留 SL）。")
            # 只掛 SL
            sl_id, _ = _attach_exits_after_fill(
                symbol,
                position_side,
                formatted_sl_price,
                formatted_sl_price,
                entry_order_id=order_id
            )
            try:
                notify_user(
                    text=(f"[warning] 價格過近，僅掛 SL 以避免即刻觸發 TP\n"
                        f"• 標的: {symbol}\n"
                        f"• 方向: {action} ({position_side})\n"
                        f"• SL: {formatted_sl_price} (ID: {sl_id})"),
                    loop=event_loop
                )
            except Exception:
                pass
            print("="*30 + "\n")
            return
    except Exception as e:
        print(f"[warning] 當前價查詢失敗，仍將嘗試掛 SL/TP：{e}")

    # 6) 正式掛上 SL/TP（條件關倉單）
    sl_id, tp_id = _attach_exits_after_fill(
        symbol,
        position_side,
        formatted_sl_price,
        formatted_tp_price,
        entry_order_id=order_id
    )
    try:
        notify_user(
            text=(f"📎 已掛上風控單 (SL/TP)\n"
                f"• 標的: {symbol}\n"
                f"• 方向: {action} ({position_side})\n"
                f"• SL: {formatted_sl_price} (ID: {sl_id})\n"
                f"• TP: {formatted_tp_price} (ID: {tp_id})\n"
                f"• 來源訊號: {signal_text}"),
            loop=event_loop
        )
    except Exception:
        pass
    print("="*30 + "\n")



# (v32: 監聽所有訊息)
@client.on(events.NewMessage()) 
async def handle_new_channel_message(event):

    message_text = event.message.message
    if not message_text:
        return

    # 忽略所有機器人帳號發出的訊息（避免自己的 Bot 推播被吃進來）
    try:
        sender = await event.get_sender()
        if getattr(sender, "bot", False):
            return
    except Exception:
        # 若取 sender 失敗，保守處理：若訊息標記有 via_bot_id 也忽略
        if getattr(event.message, "via_bot_id", None):
            return

    # 將中文俗稱（如 大餅/姨太/以太/二餅）正規化為 BTC/ETH
    normalized_text = normalize_aliases(message_text)

    channel_title = "未知聊天"
    if event.chat:
        channel_title = getattr(event.chat, 'title', getattr(event.chat, 'username', str(event.chat.id)))

    is_saved_message = (
        event.is_private 
        and event.message.out == True 
        and event.peer_id.user_id == event.message.from_id.user_id
    )
    if is_saved_message:
        channel_title = "Saved Messages (自我測試)"
    elif event.message.out == True:
         channel_title = f"(我發送到 {channel_title} 的訊息)"

    # --- 便利指令優先處理（不可被預過濾擋掉） ---
    cmd_lower = message_text.strip().lower()
    if cmd_lower in ("/where", "/id", "/ping"):
        try:
            if cmd_lower == "/ping":
                await event.reply("pong ✅")
                return
            # /where 或 /id：回覆 chat_id 與標題
            chat_id = event.chat_id
            reply = (
                f"📍 chat info\n"
                f"• title: {channel_title}\n"
                f"• chat_id: {chat_id}\n"
                f"• 用法：將 NOTIFY_TARGET 設為 {chat_id}（整數）最穩定\n"
                f"  也可用本群的 @username 或邀請連結"
            )
            await event.reply(reply)
        except Exception as e:
            await event.reply(f"[warning] 讀取 chat_id 失敗：{e}")
        return



    print(f"\n--- 監聽到來自 [{channel_title}] 的新訊息 ---")
    print(f"原始訊息: {message_text}")
    if normalized_text != message_text:
        print(f"正規化: {normalized_text}")
    print()
    
    loop = asyncio.get_event_loop()

    # --- [warning] v32 工作流 Step 1: 解析 ---
    trade_command_1 = await loop.run_in_executor(None, parse_signal_with_llm, normalized_text)
    print(f"LLM 解析結果 (1/2): {trade_command_1}")
    
    action = trade_command_1.get('action')
    if action and action != "NONE":
        symbol = trade_command_1.get('symbol')
        # 若 LLM 給出 BUY/SELL 但 symbol 缺失或無效，直接忽略
        if action in ("BUY", "SELL") and (not symbol or not is_valid_symbol(symbol)):
            print(f"[error] 訊號拒絕：無效或缺失的 symbol（{symbol}），忽略。")
            return
        print("[info] 偵測到有效訊號，正在提交 LLM 進行二次驗證 (策略補充)...")
        entry_price = trade_command_1.get('entry_price') # 可能是 null
        
        if not symbol:
            print("[error] 訊號不完整 (缺少 Symbol)，已忽略。")
            return

        # --- [warning] v32 工作流 Step 1.5: 處理市價單 ---
        is_market_order = (entry_price is None)
        if is_market_order:
            print("[info] 偵測到【市價單】，正在獲取當前市價...")
            current_market_price_task = loop.run_in_executor(None, get_binance_market_price, symbol)
            current_market_price = await current_market_price_task
            
            if not current_market_price:
                print(f"[error] 交易拒絕：無法獲取 {symbol} 的市價。")
                return
            print(f"   [Binance] {symbol} 當前市價: {current_market_price}")
            trade_command_1['entry_price'] = current_market_price
            entry_price = current_market_price 
        
        if not entry_price:
            print("[error] 訊號不完整 (缺少 Entry Price)，已忽略。")
            return

        # --- [warning] v32 工作流 Step 2: 獲取 K 線 ---
        klines_data = await loop.run_in_executor(None, get_binance_klines_for_llm, symbol)
        
        # --- [warning] v33 工作流 Step 3: 風控補齊（可選 LLM / Python） ---
        if USE_PY_RISK_MANAGER:
            print("[Risk-Py] 使用 Python 計算止損/止盈（略過 LLM 第二階段）...")
            final_leverage = apply_leverage_override(symbol, trade_command_1.get('leverage'))
            try:
                dec_entry_price = Decimal(str(entry_price))
                user_sl = trade_command_1.get('stop_loss')
                user_tp = trade_command_1.get('take_profit')
                sl_dec, tp_dec, warn_msgs = select_sl_tp_with_user_pref(symbol, action, dec_entry_price, user_sl, user_tp)
                for w in warn_msgs:
                    print(f"[warning] 風控提醒：{w}")
                final_stop_loss = str(sl_dec)
                final_take_profit = str(tp_dec)
            except Exception as e:
                print(f"[error] 交易拒絕：Python 止損/止盈計算失敗: {e}")
                return
        else:
            validation_json = await loop.run_in_executor(None, complete_trade_with_llm, trade_command_1, klines_data)
            print(f"LLM 驗證結果 (2/2): {validation_json}")
            if not (validation_json and validation_json.get("approve") == True):
                reason = "LLM 驗證失敗"
                if validation_json:
                    reason = validation_json.get('reason', 'LLM 返回無效 JSON')
                print(f"[error] LLM 已拒絕交易 (理由: {reason})。已取消下單。")
                return
            final_stop_loss = validation_json.get('stop_loss') or trade_command_1.get('stop_loss')
            final_leverage = apply_leverage_override(symbol, validation_json.get('leverage') or trade_command_1.get('leverage'))
            final_take_profit = validation_json.get('take_profit') or trade_command_1.get('take_profit')
            if final_stop_loss is None or final_take_profit is None:
                print(f"[error] 交易拒絕：LLM 未能設定有效的 SL/TP。")
                return
            try:
                sl_dec, tp_dec, warn_msgs = sanitize_targets(symbol, action, entry_price, final_stop_loss, final_take_profit)
                for w in warn_msgs:
                    print(f"[warning] 風控提醒：{w}")
                final_stop_loss = str(sl_dec)
                final_take_profit = str(tp_dec)
            except Exception as e:
                print(f"[error] 交易拒絕：目標價矯正失敗：{e}")
                return

        print(f"[info] 風控補齊完成（SL/TP 已確定）。")

        # --- [warning] v33 工作流 Step 4: Python 倉位計算 ---
        try:
            eprice_dec = Decimal(str(entry_price))
            lev_dec = Decimal(str(final_leverage))
            price_diff = abs(Decimal(str(entry_price)) - Decimal(str(final_stop_loss)))
            if price_diff == 0:
                print(f"[error] 交易拒絕：入場價和止損價相同！")
                return

            max_margin_amt = Decimal(str(total_available_margin)) * Decimal(str(MAX_INITIAL_MARGIN_PCT))

            if POSITION_SIZING_MODE == 'margin':
                # 以「初始保證金 = 可用餘額 * MAX_INITIAL_MARGIN_PCT」計算部位
                qty_by_margin = (max_margin_amt * lev_dec) / eprice_dec
                final_quantity = float(qty_by_margin)
                planned_initial_margin = (eprice_dec * Decimal(str(final_quantity))) / lev_dec
                planned_risk_amount = Decimal(str(final_quantity)) * price_diff  # 用於對照說明
                sizing_note = "（按初始保證金 3% 計算）"
            else:
                # 原本的「每筆風險金額」算法
                risk_amount_usdt = Decimal(str(total_available_margin)) * Decimal(str(RISK_PER_TRADE_PERCENT))
                final_quantity = float(risk_amount_usdt / price_diff)
                # 仍受初始保證金上限保護
                qty_cap_by_margin = (max_margin_amt * lev_dec) / eprice_dec
                if Decimal(str(final_quantity)) > qty_cap_by_margin:
                    print(f"[warning] 已啟動保證金上限保護：每筆初始保證金 ≤ {MAX_INITIAL_MARGIN_PCT*100:.1f}% 可用餘額。")
                    print(f"   原計算數量: {final_quantity:.6f}，上限數量: {qty_cap_by_margin:.6f}")
                    final_quantity = float(qty_cap_by_margin)
                planned_initial_margin = (eprice_dec * Decimal(str(final_quantity))) / lev_dec
                planned_risk_amount = Decimal(str(final_quantity)) * price_diff
                sizing_note = "（按每筆風險金額計算）"

            print(f"--- Python 倉位計算 ---")
            print(f"   總可用保證金: {total_available_margin:.2f} USDT")
            print(f"   模式: {POSITION_SIZING_MODE} {sizing_note}")
            print(f"   初始保證金目標: {MAX_INITIAL_MARGIN_PCT*100:.1f}% → 計劃使用 ≈ {planned_initial_margin:.4f} USDT")
            try:
                est_pct = (planned_initial_margin / Decimal(str(total_available_margin))) * Decimal('100')
                print(f"   預估初始保證金占比: {est_pct:.4f}% （上限 {MAX_INITIAL_MARGIN_PCT*100:.2f}%）")
            except Exception:
                pass
            print(f"   入場價: {entry_price}, 止損價: {final_stop_loss}")
            print(f"   價差(至SL): {price_diff}")
            print(f"   理論最大虧損(至SL): {planned_risk_amount:.4f} USDT")
            print(f"   槓桿: {int(final_leverage)}x")
            print(f"   ==> 計算數量: {final_quantity:.6f} {symbol.replace('USDT', '')}")

            # 在送交下單前，保留觸發下單的原訊號（使用正規化後的文字較穩定）
            signal_text = normalized_text.strip()

            final_trade_command = {
                "action": action,
                "symbol": symbol,
                "entry_price": None if is_market_order else entry_price,
                "take_profit": final_take_profit,
                "stop_loss": final_stop_loss,
                "leverage": int(final_leverage),
                "quantity": final_quantity,
                "signal_text": signal_text
            }

            await loop.run_in_executor(None, execute_trade, final_trade_command, loop)
        except Exception as e:
            print(f"[error] 交易拒絕：Python 倉位計算失敗: {e}")
            
    else:
        print("[info] 非交易訊號，已忽略。")


# --- 6. 🚀 啟動腳本---

async def main_telethon():
    """Telethon 啟動 + 啟動時對帳/恢復監控"""
    print("[info] 正在啟動 Telethon 客戶端...")
    await client.start()
    print("[info] 客戶端已登入。")

    # 取得正在運行中的事件迴圈
    loop = asyncio.get_running_loop()

    # 1) 載入本地狀態
    try:
        load_state()
    except Exception as e:
        print(f"[warning] 載入狀態檔失敗：{e}")

    # 2) 啟動週期性清理孤兒單任務
    asyncio.create_task(_periodic_reconcile_task(600))
    # 3) 啟動每日盈虧通知
    asyncio.create_task(daily_pnl_notifier('Asia/Taipei', 0, 0))

    print(f"[info] 正在監聽 *所有* 訊息 (包含傳出)...")
    await client.run_until_disconnected()

async def _periodic_reconcile_task(interval_sec: int = 600):
    """
    週期性清理孤兒單與逾時開倉單（慢速穩定掃描）：預設每 10 分鐘跑一次。
    """
    while True:
        try:
            await asyncio.get_event_loop().run_in_executor(None, reconcile_on_start, asyncio.get_event_loop())
        except Exception as e:
            print(f"[warning] 週期性 Reconcile 失敗：{e}")
        try:
            print("[info] 週期性清理本地 json 單據紀錄")
            await asyncio.get_event_loop().run_in_executor(None, resume_trades_from_state, loop)
        except Exception as e:
            print(f"[warning] 本地端單據清理失敗：{e}")
        # 加一點小抖動，避免每次都撞在同一時間窗（不用額外 import random）
        jitter = (int(time.time()) % 7)  # 0~6 秒
        await asyncio.sleep(interval_sec + jitter)

if __name__ == '__main__':

    if binance_client is None:
        print("[error] 幣安客戶端未初始化。請檢查您的 'binance.txt' 和 API Key 權限。")
        exit()
    if client is None:
        print("[error] Telethon 客戶端未初始化。請檢查您的 'telegram.txt'。")
        exit()

    print("[warning] 警告：機器人現在已上線。")

    loop = client.loop

    try:
        # 由 main_telethon() 負責：載入狀態、啟動時對帳、恢復監控
        loop.run_until_complete(main_telethon())
    except KeyboardInterrupt:
        print("\n[warning] 手動停止腳本。")
    except Exception as e:
        if "ApiIdInvalidError" in str(e) or "ApiId" in str(e):
            print("\n[error] 錯誤：API_ID 或 API_HASH 不正確。請檢查您的 'telegram.txt'。")
        else:
            print(f"\n[error] 發生未捕獲的錯誤: {e}")
    finally:
        if client and client.is_connected():
            loop = asyncio.get_event_loop()
            if loop.is_running():
                loop.create_task(client.disconnect())
            else:
                loop.run_until_complete(client.disconnect())
        print("[info] 客戶端已斷開連接。")