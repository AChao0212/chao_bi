import re
import json
import requests
from config import OLLAMA_API_URL, OLLAMA_TIMEOUT, OLLAMA_PARSER_MODEL, OLLAMA_RISK_MODEL


# === [llm_client] LLM Prompt 與呼叫 ===
# --- 2. 💡 黃金 Prompt (LLM Call 1: 解析) ---
# (v32: 強化市價單範例)
MASTER_PROMPT_TEMPLATE = """
你是一個專業、精確的交易訊號解析 AI。
你的唯一任務是分析以下文字，並將其嚴格轉換為 JSON 格式。

【JSON 欄位規則】
- action: "BUY" (多), "SELL" (空), 或 "NONE" (非訊號)。
- symbol: 必須是標準幣安合約交易對 (例如 "BTCUSDT", "ETHUSDT")。
- entry_price: 訊號的入場價格。如果提及 "市價"，必須為 null。
- take_profit: 【第一個】止盈價格。
- stop_loss: 止損價格。
- leverage: 槓桿倍數 (僅數字)。

【解析規則】
1.  **嚴格遵守格式**。如果訊息只是聊天或分析 (例如 "BTC 猛拉起飛中")，'action' 必須是 "NONE"，另外請自動忽略訊息中的表情符號。
2.  **幣種**: (如 "BTC", "ETH", "SOL", "pippin", "GIGGLE", "TRUMP", "TRUST", "币安人生") 自動附加 "USDT"。
3.  **方向**: "短" 或 "空" 等同 "SELL"。 "長" 或 "多" 等同 "BUY"。 如果沒有明確的指示，'action' 必須是 "NONE"。
4.  **進場 (entry_price)**:
    - 如果是區間 (例如 "146.23-141.70")，請**只取第一個數字** (例如 "146.23")。
    - 如果是 "市價" (例如 "pippin 市價多" 或 "btc 市價空")，`entry_price` 必須設為 `null`。
5.  **止盈 (take_profit)**:
    - 如果有多個止盈點 (例如 "150.0 \n 155.6")，請**只取第一個數字** (例如 "150.0")。
    - 如果未提及，設為 `null`。
6.  **止損 (stop_loss)**:
    - 如果未提及，設為 `null`。
7.  **槓桿 (leverage)**:
    - (例如 "20x" 或 "50x") 應只提取數字 (例如 20 或 50)。
    - 如果未提及，設為 `null`。
8.  **只回答 JSON 格式的文字**，不要有任何額外的解釋。

【範例】
---
訊息: "#SOL 多 \n進場：146.23-141.70\n止盈：\n150.0\n155.6\n止損:136.8"
JSON: {{"action": "BUY", "symbol": "SOLUSDT", "entry_price": "146.23", "take_profit": "150.0", "stop_loss": "136.8", "leverage": null}}
---
訊息: "#ETH 3500 多 20x\n止盈 3600"
JSON: {{"action": "BUY", "symbol": "ETHUSDT", "entry_price": "3500", "take_profit": "3600", "stop_loss": null, "leverage": "20"}}
---
訊息: "pippin 市價多"
JSON: {{"action": "BUY", "symbol": "PIPPINUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
訊息: "btc 市價空"
JSON: {{"action": "SELL", "symbol": "BTCUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
訊息: "AIA 輕倉空"
JSON: {{"action": "SELL", "symbol": "AIAUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
訊息: "#GIGGLE 150上方轻仓追空 止损160"
JSON: {{"action": "SELL", "symbol": "GIGGLEUSDT", "entry_price": "150", "take_profit": null, "stop_loss": "160", "leverage": null}}
---
訊息: "#BTC 104000空单目前浮盈1100点🌟，需要进阶群的联系：\n跟单：@qihangbtc1\n双向：@qihangbtcBOT"
JSON: {{"action": "NONE", "symbol": "BTCUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
訊息: "#MITO\n内部群20分鐘拿下TP 2止盈 \n獲利3倍利潤\n\n入群跟單： @cryptoanan0"
JSON: {{"action": "NONE", "symbol": "MITOUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
訊息: "#trump 剛好觸及Tp 2"
JSON: {{"action": "NONE", "symbol": "TRUMPUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
訊息: "已翻倉！速減倉"
JSON: {{"action": "NONE", "symbol": null, "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
訊息: "#PHA 猛拉起飛中"
JSON: {{"action": "NONE", "symbol": "PHAUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
訊息: "#trump  可惜，墨跡了一天還是沒能反彈出局，這單我的問題止損出"
JSON: {{"action": "NONE", "symbol": "TRUMPUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---

【任務】
請解析以下訊息：

"{user_message}"
"""

# --- ⚠️ 黃金 Prompt (LLM Call 2: 策略補充) ---
# (v32: 修正 SL/TP 邏輯錯誤)
RISK_MANAGER_PROMPT_TEMPLATE = """
你是一位資深的量化交易策略【補充員】，專精於短線 (Scalping) 交易。

【情境】
一個高勝率 (75%+) 的訊號源提供了以下的*不完整*交易建議:
{trade_json}

【當前市場數據】
這是 {symbol} 最新的【5 分鐘 K 線】數據 (OHLCV - 開/高/低/收/量):
{klines_data}

【嚴格規則】
1) 我們【傾向執行】此訊號，你的任務是【補齊數值】而非否決。
2) 檢查止損方向（最重要）：
   - entry_price = {entry_price}
   - action = {action}
   - BUY → stop_loss 必須 < entry_price
   - SELL → stop_loss 必須 > entry_price
   - 若原 stop_loss 方向錯，務必重算。
3) 若 stop_loss 為 null 或方向錯誤，請根據 K 線支撐/阻力重算一個合理的止損價。
4) take_profit 僅能用下列公式計算，不可自行加入倍數或單位：
   - BUY → TP = entry_price + 1.5 * (entry_price - stop_loss)
   - SELL → TP = entry_price - 1.5 * (stop_loss - entry_price)
5) 僅在 leverage 為 null 時，填入 50。
6) 嚴格的「最小距離」規則（避免秒觸發）：
   - 定義 min_stop = max(0.4% * entry_price, 1 * 最近的 ATR(14, 5m))
   - BUY → (entry_price - stop_loss) 必須 ≥ min_stop
   - SELL → (stop_loss - entry_price) 必須 ≥ min_stop
   - 若不滿足，請調整 stop_loss 以滿足此條件
7) 僅回傳 JSON，數字請直接用十進位字面值（最多 4 位小數），不得加千分位或多餘的 0。

【輸出格式】
{"approve": true, "reason": "訊號可執行。已根據 5m K 線補充 SL/TP。", "stop_loss": "xxxxx.xxxx", "leverage": 50, "take_profit": "yyyyy.yyyy"}
"""

# --- 3. 🧠 Ollama 函數 ---
def call_ollama(prompt_text, model_name):
    """(此函數不變)"""
    data = { 
        "model": model_name, 
        "prompt": prompt_text, 
        "stream": False, 
        "options": {"temperature": 0.0} 
    }
    
    if "gemma" in model_name:
        if not prompt_text.strip().endswith("JSON:"):
             prompt_text += "\nJSON:"

    try:
        response = requests.post(OLLAMA_API_URL, json=data, timeout=OLLAMA_TIMEOUT) 
        response.raise_for_status()
        response_text = response.json().get('response', '{}')
        json_match = re.search(r'\{.*\}', response_text, re.DOTALL)
        if json_match:
            response_text = json_match.group(0)
        return json.loads(response_text)
    except requests.exceptions.ReadTimeout:
        print(f"❌ [LLM 錯誤]: Ollama 處理時間超過 {OLLAMA_TIMEOUT} 秒 (Read timed out)。")
        return None
    except Exception as e:
        print(f"❌ [LLM 錯誤]: {e}")
        return None

def parse_signal_with_llm(message_text: str) -> dict:
    """(此函數不變)"""
    print(f"[LLM 1/2: 解析中 (使用 {OLLAMA_PARSER_MODEL})...]")
    prompt = MASTER_PROMPT_TEMPLATE.format(user_message=message_text)
    result = call_ollama(prompt, OLLAMA_PARSER_MODEL) 
    return result if result else {"action": "NONE"}

def complete_trade_with_llm(trade_command: dict, klines_data: str) -> dict:
    """(v32) LLM Call 2: 策略補充"""
    print(f"[LLM 2/2: 策略補充中 (使用 {OLLAMA_RISK_MODEL})...]")
    trade_json = json.dumps(trade_command)
    prompt = RISK_MANAGER_PROMPT_TEMPLATE.format(
        trade_json=trade_json,
        symbol=trade_command['symbol'],
        klines_data=klines_data,
        entry_price=trade_command['entry_price'],
        action=trade_command['action']
    )
    result = call_ollama(prompt, OLLAMA_RISK_MODEL)
    return result if result else {"approve": False, "reason": "LLM 驗證失敗"}