# config.py
import os
from decimal import Decimal

def load_api_keys(*files):
    """從多個檔案載入 KEY=VALUE 格式的設定。"""
    config = {}
    for file_name in files:
        if not os.path.exists(file_name):
            print(f"[error] 警告：找不到金鑰檔案 '{file_name}'，將跳過。")
            continue
        try:
            with open(file_name, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    try:
                        key, value = line.split('=', 1)
                        key = key.strip()
                        value = value.strip().strip("'\"")
                        config[key] = value
                    except ValueError:
                        print(f"⚠️ 格式錯誤：跳過 '{file_name}' 中的行: {line}")
            print(f"[info] 已成功從 '{file_name}' 載入金鑰。")
        except Exception as e:
            print(f"[error] 讀取 '{file_name}' 時發生錯誤: {e}")
    return config

# 這裡依照你原本的設定檔路徑
api_config = load_api_keys(
    '/Users/hao/.secret/telegram.txt',
    '/Users/hao/.secret/binance.txt'
)

STATE_FILE_PATH = os.path.join(os.path.dirname(__file__), "chao_bi_state.json")

# Telegram 設定
API_ID = api_config.get('API_ID')
API_HASH = api_config.get('API_HASH')
BOT_TOKEN = api_config.get('BOT_TOKEN')
BOT_CHAT_ID = api_config.get('BOT_CHAT_ID')
CLIENT_SESSION_NAME = 'chao_bi'

# Ollama
OLLAMA_API_URL = 'http://192.168.50.1:11434/api/generate'
OLLAMA_TIMEOUT = 180
OLLAMA_PARSER_MODEL = 'gpt-oss:20b'
OLLAMA_RISK_MODEL = 'gpt-oss:20b'

# Binance
BINANCE_API_KEY = api_config.get('BINANCE_API_KEY')
BINANCE_API_SECRET = api_config.get('BINANCE_API_SECRET')
REAL_FUTURES_BASE_URL = "https://fapi.binance.com"

# 風險參數
RISK_PER_TRADE_PERCENT = 0.03        # 3%
DEFAULT_LEVERAGE = 50
LEVERAGE_OVERRIDES = {
    'BTCUSDT': 125,
    'ETHUSDT': 125,
    'BNBUSDT': 75,
    'SOLUSDT': 100,
}

# 風控補強：初始保證金上限與 TP 計算的 RR 參數
MAX_INITIAL_MARGIN_PCT = 0.03  # 每筆訂單的初始保證金上限（占可用餘額的比例），例：3%
RR_DEFAULT = Decimal('1.5')    # 預設 RR 倍數（TP = entry ± 1.5 * distance）
RR_MAX = Decimal('3.0')        # 允許 LLM 給的 TP 與預設值距離差的上限倍數
# --- 下單部位大小模式 ---
# 'risk'  : 以「每筆風險金額 = 可用餘額 * RISK_PER_TRADE_PERCENT」計算（原邏輯）
# 'margin': 以「每筆初始保證金 = 可用餘額 * MAX_INITIAL_MARGIN_PCT」計算（符合你要的 3% 用量）
POSITION_SIZING_MODE = 'margin'  # ← 依需求改為 'risk' 或 'margin'

# ---- 風控策略切換與參數----
# 若設為 True，改用 Python 決策來計算 SL/TP（捨棄第二次 LLM 補齊）
USE_PY_RISK_MANAGER = True
# 最小止損距離（以入場價百分比）；避免 TP/SL 太貼近而「秒觸發」
MIN_STOP_DISTANCE_PCT = Decimal('0.004')   # 0.4%
# ATR 參數（以 5 分鐘 K 線計算）
ATR_PERIOD = 14
ATR_K = Decimal('1.0')  # 止損距離至少為 ATR * ATR_K
# 12 小時未成交自動撤單（秒）
AUTO_CANCEL_SECONDS = 12 * 60 * 60
# 監控輪詢間隔（秒）
ORDER_MONITOR_INTERVAL = 30
# Reconcile 診斷輸出（True=詳細列印每個 symbol 的錯誤；False=靜默）
RECONCILE_VERBOSE = True
# 初始短期輪詢（剛下LIMIT單時在主流程內的等待）
INITIAL_FILL_WAIT_SECONDS = 60     # 最長等待時間
INITIAL_POLL_INTERVAL = 1.0        # 每次查詢間隔（原本是0.5秒；為降低API壓力改為1秒）

# ---- 慢速但穩定的 Reconcile 模式（回滾版） ----
SLOW_STABLE_RECONCILE = True      # True = 使用逐 symbol 掃描（SDK），雖慢但穩
PER_SYMBOL_SLEEP_SEC = 0       # 逐 symbol 查詢之間休息，降低被 WAF/限流
PER_SYMBOL_RETRY = 2              # 每個 symbol 失敗時重試次數