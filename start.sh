#!/bin/bash

# 使用說明函式
print_usage() {
    echo "用法："
    echo "  ./start.sh            - 啟動機器人（需先完成 init）"
    echo "  ./start.sh init       - 初始化：建立環境 + 安裝套件 + Telegram 登入"
    echo "  ./start.sh delete     - 刪除所有檔案與設定"
}

# 處理 delete 指令
if [ "$1" = "delete" ]; then
    echo "確定要刪除所有檔案與設定嗎？此操作無法復原！(y/n)"
    read -r confirm
    if [ "$confirm" != "y" ]; then
        echo "已取消刪除操作。"
        exit 0
    fi
    echo "正在停止機器人並刪除虛擬環境..."
    pkill -f "python3 chao_bi.py" 2>/dev/null

    rm -rf .venv
    echo "正在刪除 ~/.secret ..."
    rm -rf ~/.secret

    echo "正在刪除此專案資料夾（包括 start.sh 自身）..."
    rm -rf "$(dirname "$0")"

    echo "刪除完成。"
    exit 0
fi

# init 初始化模式
if [ "$1" = "init" ]; then
    echo "====== 初始化 Chao_Bi ======"

    echo "建立虛擬環境..."
    python3 -m venv .venv
    source .venv/bin/activate

    echo "安裝所需套件..."
    pip install requests telethon binance-futures-connector

    echo "建立 ~/.secret ..."
    mkdir -p ~/.secret
    touch ~/.secret/telegram.txt
    touch ~/.secret/binance.txt

    echo "檢查 API KEY 設定狀態..."

    # 檢查 telegram.txt 和 binance.txt 是否已有內容
    if [ ! -s ~/.secret/telegram.txt ] || [ ! -s ~/.secret/binance.txt ]; then
        echo "尚未完成 API KEY 設定。"
        echo "請將您的 Telegram 與 Binance API 金鑰分別填入："
        echo "  ~/.secret/telegram.txt"
        echo "  ~/.secret/binance.txt"
        echo "填寫完成後，請再次執行： ./start.sh init"
        exit 1
    fi

    echo "已偵測到 API KEY，接下來將啟動 Telegram 首次登入流程..."

    python3 login_once.py

    echo "初始化完成！之後請使用： ./start.sh"
    exit 0
fi

if [ "$1" = "stop" ]; then
    echo "正在停止機器人..."
    pkill -f "python3 chao_bi.py" 2>/dev/null
    echo "機器人已停止。"
    exit 0
fi

# 日常啟動模式
if [ -z "$1" ]; then
    if [ ! -d .venv ]; then
        echo "尚未初始化！請先執行： ./start.sh init"
        exit 1
    fi

    source .venv/bin/activate

    if [ ! -f chao_bi.session ]; then
        echo "偵測到尚未登入 Telegram，請先執行： ./start.sh init"
        exit 1
    fi
    
    if pgrep -f "python3 chao_bi.py" > /dev/null; then
        echo "機器人已在運行中，無需重複啟動。"
        exit 0
    fi
    python3 chao_bi.py > log.txt 2>&1 &
    echo "機器人已啟動，日誌輸出至 log.txt"
    exit 0
fi

# 若參數無效 → 顯示用法
print_usage
exit 0