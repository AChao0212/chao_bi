# config.py
import os
from decimal import Decimal

def load_api_keys(*files):
    """從多個檔案載入 KEY=VALUE 格式的設定。"""
    config = {}
    for file_name in files:
        if not os.path.exists(file_name):
            print(f"[error] [config]: 找不到金鑰檔案 '{file_name}'，請確認是否存在。")
            exit()
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
                        print(f"[error] [config]: 格式錯誤，跳過 '{file_name}' 中的行: {line}")
            print(f"[messg] [config]: 已成功從 '{file_name}' 載入金鑰。")
        except Exception as e:
            print(f"[error] [config]: 讀取 '{file_name}' 時發生錯誤: {e}")
    return config

# 這裡依照你原本的設定檔路徑
api_config = load_api_keys(
    '/home/pinhao/.secret/telegram.txt',
    '/home/pinhao/.secret/binance.txt'
)

STATE_FILE_PATH = os.path.join(os.path.dirname(__file__), "chao_bi_state.json")
TRADE_LOG_CSV_PATH = os.path.join(os.path.dirname(__file__), "trade_log.csv")

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
    'BTCUSDT': 150,
    'ETHUSDT': 150,
    'BNBUSDT': 75,
    'SOLUSDT': 100,
}

# 風控補強：初始保證金上限與 TP 計算的 RR 參數
MAX_INITIAL_MARGIN_PCT = 0.03  # 每筆訂單的初始保證金上限（占可用餘額的比例），例：3%
RR_DEFAULT = Decimal('2.5')    # 預設 RR 倍數（TP = entry ± 1.5 * distance）
RR_MAX = Decimal('3.0')        # 允許 LLM 給的 TP 與預設值距離差的上限倍數

# ---- 風控策略切換與參數----
# 若設為 True，改用 Python 決策來計算 SL/TP（捨棄第二次 LLM 補齊）
USE_PY_RISK_MANAGER = True
# 最小止損距離（以入場價百分比）；避免 TP/SL 太貼近而「秒觸發」
MIN_STOP_DISTANCE_PCT = Decimal('0.004')   # 0.4%
# ATR 參數（以 5 分鐘 K 線計算）
ATR_PERIOD = 14
ATR_K = Decimal('1.5')  # 止損距離至少為 ATR * ATR_K
# 12 小時未成交自動撤單（秒）
AUTO_CANCEL_SECONDS = 12 * 60 * 60
# 監控輪詢間隔（秒）
ORDER_MONITOR_INTERVAL = 30
# Reconcile 診斷輸出（True=詳細列印每個 symbol 的錯誤；False=靜默）
RECONCILE_VERBOSE = True