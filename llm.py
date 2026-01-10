"""
LLM Integration Module.

Provides signal parsing using local LLM (Ollama).
"""

import re
import json
import requests
from config import OLLAMA_API_URL, OLLAMA_TIMEOUT, OLLAMA_PARSER_MODEL
from logger import ModuleLogger

# Initialize logger
log = ModuleLogger("llm")

# =============================================================================
# PROMPT TEMPLATE
# =============================================================================

SIGNAL_PARSER_PROMPT = """
You are a professional, precise trading signal parsing AI.
Your only task is to analyze the following text and strictly convert it to JSON format.

【JSON Field Rules】
- action: "BUY" (long), "SELL" (short), or "NONE" (not a signal).
- symbol: Must be a standard Binance contract pair (e.g., "BTCUSDT", "ETHUSDT").
- entry_price: Entry price. If "market price" is mentioned, must be null.
- take_profit: The FIRST take-profit price.
- stop_loss: Stop-loss price.
- leverage: Leverage multiplier (number only).

【Parsing Rules】
1. **Strictly follow format**. If message is just chat or analysis (e.g., "BTC surging"), 'action' must be "NONE". Ignore emojis.
2. **Symbol**: (e.g., "BTC", "ETH", "SOL") automatically append "USDT".
3. **Direction**: "空" equals "SELL". "多" equals "BUY".
4. **Entry (entry_price)**:
   - If range (e.g., "146.23-141.70"), take ONLY the first number (e.g., "146.23").
   - If "market price" (e.g., "pippin market long"), `entry_price` must be `null`.
5. **Take Profit (take_profit)**:
   - If multiple TPs (e.g., "150.0 \\n 155.6"), take ONLY the first number (e.g., "150.0").
   - If not mentioned, set to `null`.
6. **Stop Loss (stop_loss)**:
   - If not mentioned, set to `null`.
7. **Leverage (leverage)**:
   - (e.g., "20x" or "50x") extract number only (e.g., 20 or 50).
   - If not mentioned, set to `null`.
8. **Only answer in JSON format**, no extra explanation.

【Examples】

- Trading signals

---
Message: "#SOL long \\nEntry: 146.23-141.70\\nTP:\\n150.0\\n155.6\\nSL:136.8"
JSON: {{"action": "BUY", "symbol": "SOLUSDT", "entry_price": "146.23", "take_profit": "150.0", "stop_loss": "136.8", "leverage": null}}
---
Message: "#ETH 3500 long 20x\\nTP 3600"
JSON: {{"action": "BUY", "symbol": "ETHUSDT", "entry_price": "3500", "take_profit": "3600", "stop_loss": null, "leverage": "20"}}
---
Message: "pippin market long"
JSON: {{"action": "BUY", "symbol": "PIPPINUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
Message: "#FIL light long around 2.09"
JSON: {{"action": "BUY", "symbol": "FILUSDT", "entry_price": "2.09", "take_profit": null, "stop_loss": null, "leverage": null}}
---
Message: "#GIGGLE short above 150 SL 160"
JSON: {{"action": "SELL", "symbol": "GIGGLEUSDT", "entry_price": "150", "take_profit": null, "stop_loss": "160", "leverage": null}}
---
Message: "#evaa market long small position"
JSON: {{"action": "BUY", "symbol": "EVAAUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---

- Advertisement/Non-signal messages

Message: "#BTC 104000 short floating profit 1100 points, contact for premium group"
JSON: {{"action": "NONE", "symbol": "BTCUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
Message: "#MITO\\nInternal group hit TP2 in 20 mins\\n3x profit\\nJoin: @cryptoanan0"
JSON: {{"action": "NONE", "symbol": "MITOUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
Message: "#trump just hit TP2"
JSON: {{"action": "NONE", "symbol": "TRUMPUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
Message: "Flipped! Reduce position!"
JSON: {{"action": "NONE", "symbol": null, "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
Message: "#PHA surging!"
JSON: {{"action": "NONE", "symbol": "PHAUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---
Message: "#BTC support long precise entry +2500 points, reduce position"
JSON: {{"action": "NONE", "symbol": "BTCUSDT", "entry_price": null, "take_profit": null, "stop_loss": null, "leverage": null}}
---

【Task】
Parse the following message:

"{user_message}"
"""


# =============================================================================
# LLM API FUNCTIONS
# =============================================================================

def call_ollama(prompt: str, model: str, timeout: int, api_url: str) -> dict:
    """
    Call the Ollama API with a prompt.

    Args:
        prompt: The prompt text
        model: Model name to use
        timeout: Request timeout in seconds
        api_url: Ollama API endpoint URL

    Returns:
        Parsed JSON response or None if failed
    """
    data = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "options": {"temperature": 0.0},
    }

    try:
        response = requests.post(api_url, json=data, timeout=timeout)
        response.raise_for_status()

        response_text = response.json().get("response", "{}")

        # Extract JSON from response
        json_match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if json_match:
            response_text = json_match.group(0)

        return json.loads(response_text)

    except requests.exceptions.ReadTimeout:
        log.error(f"Ollama timeout ({timeout}s)")
        return None
    except json.JSONDecodeError as e:
        log.error(f"JSON parse error: {e}")
        return None
    except Exception as e:
        log.error(f"Ollama API error: {e}")
        return None


def parse_signal_with_llm(message_text: str) -> dict:
    """
    Parse a trading signal message using LLM.

    Args:
        message_text: The message text to parse

    Returns:
        Parsed signal dict or {"action": "NONE"} if parsing fails
    """
    log.info("Parsing signal...")

    prompt = SIGNAL_PARSER_PROMPT.format(user_message=message_text)
    result = call_ollama(prompt, OLLAMA_PARSER_MODEL, OLLAMA_TIMEOUT, OLLAMA_API_URL)

    if result:
        log.info(f"Parsed: action={result.get('action')}, symbol={result.get('symbol')}")
        return result

    return {"action": "NONE"}
