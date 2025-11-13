#!/bin/bash

if [ "$1" = "delete" ]; then
    echo "正在停止機器人並刪除虛擬環境..."
    pkill -f "python3 chao_bi.py" 2>/dev/null
    rm -rf .venv
    echo "正在刪除 ~/.secret ..."
    rm -rf ~/.secret

    echo "正在刪除此專案資料夾（包括 start.sh 自身）..."
    rm -rf "$(dirname "$0")"
    echo "已刪除虛擬環境。"
    exit 0
fi

if [ -f .venv/bin/activate ]; then
    source .venv/bin/activate
else
    echo "為您創建虛擬環境並安裝所需的依賴項..."
    python3 -m venv .venv
    source .venv/bin/activate
    pip install requests telethon binance-futures-connector
    if [ ! -d ~/.secret ]; then
        mkdir ~/.secret
    fi
    if [ ! -f ~/.secret/telegram.txt ] || [ ! -f ~/.secret/binance.txt ]; then
        touch ~/.secret/telegram.txt
        touch ~/.secret/binance.txt
        echo "請將您的 Telegram 和 Binance API 金鑰分別填入 '~/.secret/telegram.txt' 和 '~/.secret/binance.txt'，然後重新運行此腳本。"
        exit 1
    fi
fi

python3 chao_bi.py > log.txt &

echo "機器人已啟動，日誌正在記錄到 log.txt。"