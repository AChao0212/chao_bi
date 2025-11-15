# chao_bi — Telegram 自動交易機器人

```
 ██████╗██╗  ██╗ █████╗  ██████╗         ██████╗ ██╗
██╔════╝██║  ██║██╔══██╗██╔═══██╗        ██╔══██╗██║
██║     ███████║███████║██║   ██║        ██████╔╝██║
██║     ██╔══██║██╔══██║██║   ██║        ██╔══██╗██║
╚██████╗██║  ██║██║  ██║╚██████╔╝███████╗██████╔╝██║
 ╚═════╝╚═╝  ╚═╝╚═╝  ╚═╝ ╚═════╝ ╚══════╝╚═════╝ ╚═╝
```


![Python](https://img.shields.io/badge/Python-3.11+-blue)
![License](https://img.shields.io/badge/License-MIT-green)
![Telegram](https://img.shields.io/badge/Telegram-Bot-blue)
![Binance](https://img.shields.io/badge/Binance-Futures-yellow)
![Ollama](https://img.shields.io/badge/Ollama-Required-orange)

**注意：本專案需要額外安裝與設定 Ollama。請務必閱讀本文後方的「Ollama 模型準備」段落，並先完成模型下載與部署。**

chao_bi 是一個基於 Telegram + Binance Futures API 的自動交易機器人，為了讓您可以 24/7 透過 telegram 下單訊息不間斷炒幣，我們提供以下特點：
- Telegram 指令操作
- Binance Futures 下單
- LLM 交易輔助功能
- 自動化執行（systemd）
- 清晰模組化架構（多檔 Python 組成）

本專案支援兩種使用方式：
1.	git clone 整個專案使用（建議）
2.	僅下載 start.sh，自動從 GitHub 安裝程式碼

## Quick Start

```bash
git clone https://github.com/AChao0212/chao_bi.git
cd chao_bi
./start.sh init
```

## 專案結構

```text
chao_bi/
  start.sh
  chao_bi.py
  login_once.py
  binance_api.py
  telegram.py
  llm.py
  config.py
  state_store.py
  requirements.txt
  README.md
```

```chao_bi.py``` 是主程式入口，其餘 Python 檔為功能模組。

## 安裝方式

你可以依照習慣選擇其中一種方式。

方法一：使用 Git Clone（建議，適合所有用戶）

```bash
git clone https://github.com/AChao0212/chao_bi.git
cd chao_bi
chmod +x start.sh
./start.sh init
```

方法二：使用 ```start.sh```（一鍵安裝，適合不想使用 ```git``` 的用戶）
1.	建立一個資料夾
2.	把本專案中的 ```start.sh``` 放進去
3.	執行：

```bash
chmod +x start.sh
./start.sh init
```

init 會自動從 GitHub 下載本專案的程式碼。

## API Key 設定

初始化後會自動建立：

```text
~/.secret/
  ├─ telegram.txt
  └─ binance.txt
```

請手動填寫以下內容（範例值請換成你自己的）：

！！！所有金鑰檔案皆不應加入 git，請勿提交到公開平台！！！

Telegram（Telethon）

檔案：```telegram.txt```

```text
API_ID = 11111111
API_HASH = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
BOT_TOKEN = '1111111111:xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
BOT_CHAT_ID = -1111111111
```

上面的數字和金鑰都是範例，請填入您自己的參數，
格式需與程式內讀取邏輯一致。

Binance Futures

檔案：```binance.txt```

```text
BINANCE_API_KEY = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
BINANCE_API_SECRET = 'xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx'
```

上面的金鑰都是範例，請填入您自己的參數。

注意：請保持等號左右空白，並使用單引號 'value' 包覆字串，
格式與程式讀取邏輯需完全一致。

填完後重新執行：

```bash
./start.sh init
```

系統會啟動 Telegram 首次登入流程。

## 使用方式

建議想要長時間運行的用戶直接跳至下一個環節：加入 Systemd 自動啟動

▶ 啟動機器人

```bash
./start.sh
```

機器人會在背景執行，並將日誌輸出至：

```bash
log.txt
```

▶ 停止機器人

```bash
./start.sh stop
```

▶ 更新程式碼 + 套件

```bash
./start.sh update
```

內容包含：
- ```git pull```
- 重新安裝 ```requirements.txt``` 內的套件

▶ 刪除所有資料（！！！不可逆！！！）

```bash
./start.sh delete
```

會刪除：
- 背景執行程序
- ```systemd``` 服務（如有）
- 虛擬環境 ```.venv```
- ```~/.secret``` 中本服務使用的金鑰（依選項決定是否清除）
- 整個專案資料夾

##（可選）加入 Systemd 自動啟動

若你想讓機器人 24/7 不間斷運行，強烈建議啟用 systemd 模式。

執行 ```./start.sh init``` 時會詢問是否建立 ```systemd``` 服務：
```bash
/etc/systemd/system/chao_bi.service
```
啟用後可使用：

```bash
sudo systemctl status chao_bi
sudo systemctl stop chao_bi
sudo systemctl restart chao_bi
```

並會在系統開機時自動啟動。

## 日誌查看

若用 ```start.sh``` 啟動：

```bash
log.txt
```

若使用 ```systemd```：

```bash
sudo journalctl -u chao_bi -f
```

## 金鑰安全清除機制

執行 ```./start.sh delete``` 時，腳本會詢問是否清理 ```~/.secret``` 內的金鑰檔案。
- 若 ```~/.secret``` 內 只包含 ```telegram.txt``` / ```binance.txt``` → 會刪除這兩個檔案，並嘗試移除整個目錄
- 若內部還有其他檔案 → 只刪本專案使用的兩個檔案，不會影響其他服務

## 系統需求
- Python 3.11+
- Linux（建議 Ubuntu）
- systemd（選用，用於自動啟動）
- Ollama (另一台可運行 Ollama 的機器)
- git

## Ollama 模型準備

本專案需要使用 **Ollama** 作為推理引擎。  
請在啟動機器人之前，先在另一台設備安裝並啟動 Ollama，並拉取所需的模型。

### 安裝 Ollama
請參考官方說明：https://ollama.com/download

### 拉取必要模型
本專案建議使用以下模型（請依照你的機器性能選擇）：

```bash
ollama pull gpt-oss:20b
```

完成後需要將這台有 Ollama 的機器部署在 IP 位置為 ```192.168.50.1``` 的地方並開啟接收任何 IP 的功能。

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

## 尚未完成

- ollama 和本專案同時部署在同一台機器的選擇
- ollama 模型更改
- MacOS 的支援