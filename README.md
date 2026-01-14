# chao_bi — Telegram 自動交易機器人

```
 ██████╗██╗  ██╗ █████╗  ██████╗         ██████╗ ██╗
██╔════╝██║  ██║██╔══██╗██╔═══██╗        ██╔══██╗██║
██║     ███████║███████║██║   ██║        ██████╔╝██║
██║     ██╔══██║██╔══██║██║   ██║        ██╔══██╗██║
╚██████╗██║  ██║██║  ██║╚██████╔╝███████╗██████╔╝██║
 ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═════╝ ╚═╝
```


![Python](https://img.shields.io/badge/Python-3.10+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue)
![Binance](https://img.shields.io/badge/Binance-Futures-yellow)
![Ollama](https://img.shields.io/badge/Ollama-Required-orange)

**注意：本專案需要額外安裝與設定 Ollama。請務必閱讀本文後方的「Ollama 模型準備」段落，並先完成模型下載與部署。**

chao_bi 是一個基於 Telegram + Binance Futures API 的自動交易機器人，為了讓您可以 24/7 透過 telegram 下單訊息不間斷炒幣，我們提供以下特點：

- **Telegram 訊號監聽** - 自動從指定頻道/群組讀取交易訊號
- **Binance Futures 下單** - 支援 USDS-M 永續合約，Hedge Mode（雙向持倉）
- **LLM 訊號解析** - 透過 Ollama 解析中文/英文交易訊號
- **智能風控系統** - ATR 動態止損、槓桿感知風控、最大保證金風險限制
- **自動 SL/TP 管理** - 使用 Algo Orders 自動掛止損/止盈單
- **交易紀錄追蹤** - 自動記錄所有交易到 CSV，統計勝率與盈虧
- **狀態持久化** - 重啟後自動恢復追蹤中的交易
- **每日盈虧通知** - 定時推送當日交易統計
- **systemd 自動啟動** - 支援 Linux 開機自動運行

## Quick Start

```bash
git clone https://github.com/AChao0212/chao_bi.git
cd chao_bi
./start.sh init
```

## 專案結構

```text
chao_bi/
├── start.sh            # 啟動/管理腳本
├── chao_bi.py          # 主程式入口
├── binance_api.py      # Binance API 封裝（下單、風控、監控）
├── telegram.py         # Telegram 訊息處理
├── llm.py              # LLM 訊號解析
├── config.py           # 設定檔（API 金鑰路徑、風控參數）
├── state_store.py      # 交易狀態持久化
├── trade_logger.py     # 交易紀錄 CSV 寫入
├── logger.py           # 統一日誌系統
├── login_once.py       # Telegram 首次登入
├── requirements.txt    # Python 依賴
└── README.md           # 本文件
```

### 執行時產生的檔案

```text
chao_bi/
├── chao_bi.session     # Telegram 登入 session
├── chao_bi_state.json  # 追蹤中的交易狀態
├── trade_log.csv       # 已完成交易紀錄
└── log.txt             # 執行日誌
```

## 安裝方式

你可以依照習慣選擇其中一種方式。

### 方法一：使用 Git Clone（建議）

```bash
git clone https://github.com/AChao0212/chao_bi.git
cd chao_bi
chmod +x start.sh
./start.sh init
```

### 方法二：使用 `start.sh`（一鍵安裝）

1. 建立一個資料夾
2. 把本專案中的 `start.sh` 放進去
3. 執行：

```bash
chmod +x start.sh
./start.sh init
```

init 會自動從 GitHub 下載本專案的程式碼。

## API Key 設定

初始化後會自動建立：

```text
~/.secret/
├── telegram.txt
└── binance.txt
```

請手動填寫以下內容（範例值請換成你自己的）：

> **警告：所有金鑰檔案皆不應加入 git，請勿提交到公開平台！**

### Telegram（Telethon）

檔案：`~/.secret/telegram.txt`

```text
API_ID = 11111111
API_HASH = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
BOT_TOKEN = '1111111111:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
BOT_CHAT_ID = -1111111111
```

- `API_ID` / `API_HASH`：從 https://my.telegram.org 取得
- `BOT_TOKEN`：從 @BotFather 取得
- `BOT_CHAT_ID`：機器人要發送通知的聊天室 ID

### Binance Futures

檔案：`~/.secret/binance.txt`

```text
BINANCE_API_KEY = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
BINANCE_API_SECRET = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

請確保 API Key 有 **Futures 交易權限**。

填完後重新執行：

```bash
./start.sh init
```

系統會啟動 Telegram 首次登入流程。

## 風控參數設定

編輯 `config.py` 可調整風控參數：

```python
# 每筆交易風險（占可用餘額百分比）
RISK_PER_TRADE_PERCENT = 0.03        # 3%

# 預設槓桿
DEFAULT_LEVERAGE = 50

# 特定幣種槓桿覆蓋
LEVERAGE_OVERRIDES = {
    'BTCUSDT': 150,
    'ETHUSDT': 150,
    'BNBUSDT': 75,
    'SOLUSDT': 100,
}

# 每筆訂單初始保證金上限（占可用餘額百分比）
MAX_INITIAL_MARGIN_PCT = 0.06  # 6%

# 最大保證金風險（SL 觸發時損失保證金的百分比上限）
# 此參數確保不同槓桿下的風險一致性
MAX_MARGIN_RISK_PCT = Decimal('0.50')  # 50%

# 最小止損距離（以入場價百分比）
MIN_STOP_DISTANCE_PCT = Decimal('0.004')  # 0.4%

# ATR 參數（以 5 分鐘 K 線計算）
ATR_PERIOD = 14
ATR_K = Decimal('1.5')  # 止損距離至少為 ATR * 1.5

# 未成交訂單自動撤單時間
AUTO_CANCEL_SECONDS = 12 * 60 * 60  # 12 小時

# 日誌時區設定
# 可選格式：
#   - "system"          : 使用系統本地時間
#   - "Asia/Tokyo"      : 使用時區名稱
#   - "+8" 或 "-5"      : 使用 UTC 偏移量
LOG_TIMEZONE = "system"
```

## 使用方式

### 啟動機器人

```bash
./start.sh
```

機器人會在背景執行，日誌輸出至 `log.txt`。

### 停止機器人

```bash
./start.sh stop
```

### 查看日誌

```bash
./start.sh logs
# 或直接
tail -f log.txt
```

### 查看狀態

```bash
./start.sh status
```

### 更新程式碼 + 套件

```bash
./start.sh update
```

### 刪除所有資料（不可逆）

```bash
./start.sh delete
```

## Systemd 自動啟動（Linux）

執行 `./start.sh init` 時會詢問是否建立 systemd 服務。

啟用後可使用：

```bash
sudo systemctl status chao_bi
sudo systemctl stop chao_bi
sudo systemctl restart chao_bi
sudo journalctl -u chao_bi -f  # 查看日誌
```

## 交易紀錄

所有已完成的交易會記錄在 `trade_log.csv`：

| 欄位 | 說明 |
|------|------|
| timestamp | 交易完成時間 |
| symbol | 交易對 |
| position_side | LONG / SHORT |
| entry_price | 入場價格 |
| exit_price | 出場價格 |
| quantity | 數量 |
| leverage | 槓桿倍數 |
| pnl | 已實現盈虧 (USDT) |
| signal_source | 訊號來源頻道 |
| win_loss_draw | WIN / LOSS / DRAW |
| raw_signal | 原始訊號文字 |

## 系統需求

- Python 3.10+
- Linux（建議 Ubuntu）或 macOS
- systemd（選用，用於自動啟動，僅 Linux）
- Ollama（另一台可運行 Ollama 的機器）
- git

## Ollama 模型準備

本專案需要使用 **Ollama** 作為推理引擎。
請在啟動機器人之前，先在另一台設備安裝並啟動 Ollama，並拉取所需的模型。

### 安裝 Ollama

請參考官方說明：https://ollama.com/download

### 拉取必要模型

```bash
ollama pull gpt-oss:20b
```

### 設定 Ollama

1. 確保 Ollama 服務監聽所有網路介面（預設只監聽 localhost）
2. 在 `config.py` 中設定 Ollama 伺服器位址：

```python
OLLAMA_API_URL = 'http://192.168.50.1:11434/api/generate'
OLLAMA_TIMEOUT = 180
OLLAMA_PARSER_MODEL = 'gpt-oss:20b'
OLLAMA_RISK_MODEL = 'gpt-oss:20b'
```

## 技術細節

### Binance SDK

本專案使用官方 Binance SDK：
- `binance-sdk-derivatives-trading-usds-futures` v5.0.0
- 使用 Algo Orders API 處理 SL/TP 條件單

### 風控邏輯

1. **ATR 動態止損**：基於 14 週期 5 分鐘 K 線 ATR 計算止損距離
2. **槓桿感知**：根據槓桿自動調整 SL/TP 距離，確保保證金風險一致
3. **最大風險限制**：SL 觸發時最多損失 50% 保證金（可配置）
4. **最小距離保護**：止損距離至少 0.4%，避免秒觸發

### 狀態管理

- 交易狀態持久化到 `chao_bi_state.json`
- 重啟後自動恢復追蹤、補掛 SL/TP
- 定期執行 reconcile 清理孤兒訂單

## 作者

AChao0212
GitHub：https://github.com/AChao0212

## 貢獻

歡迎開 Issue 或 Pull Request！

## 免責聲明

本專案僅供學術研究、程式學習與個人自動化需求使用。
使用者需自行承擔所有風險，包括但不限於：錯誤下單、交易損失、API 金鑰管理不當、伺服器維運風險等。
開發者不對因使用本專案造成的任何損失負責，包括直接、間接、偶然或衍生性損害。

**若您啟用本專案，即代表您已理解所有風險並自行承擔後果。**

## 版本歷史

### v2.4 (2026-01-14)
- 修正 SL/TP 使用 Algo Orders API
- 新增槓桿感知風控（MAX_MARGIN_RISK_PCT）
- 修正交易紀錄 CSV 記錄問題
- 新增可配置日誌時區
- 修正 SDK 方法名稱相容性

### v2.3
- 修正未能正確紀錄的問題

### v2.2
- 修正交易紀錄未能正確紀錄盈利損失的問題並優化 prompt

### v2.1
- 修正前一版本未能正確紀錄的問題

### v2.0
- 新增交易統計功能

### v1.9
- 修正重複訊號重複開倉問題
