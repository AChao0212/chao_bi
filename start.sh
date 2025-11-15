#!/bin/bash

# 專案基本設定
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="chao_bi"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"

SECRET_DIR="$HOME/.secret"
TELEGRAM_FILE="${SECRET_DIR}/telegram.txt"
BINANCE_FILE="${SECRET_DIR}/binance.txt"

# 使用說明函式
print_usage() {
    echo "用法："
    echo "  ./start.sh            - 啟動機器人（需先完成 init）"
    echo "  ./start.sh init       - 初始化：下載/更新原始碼 + 建立環境 + 安裝套件 + Telegram 登入"
    echo "  ./start.sh stop       - 停止機器人（僅殺掉目前的 python 行程，不會停 systemd）"
    echo "  ./start.sh update     - 更新程式碼與套件"
    echo "  ./start.sh delete     - 停止並刪除所有檔案、設定與 systemd 服務"
}

# 建立 systemd 服務
create_systemd_service() {
    # 如果沒有 systemd 就直接跳過
    if ! command -v systemctl >/dev/null 2>&1; then
        echo "系統似乎沒有 systemd（找不到 systemctl），無法建立服務。"
        return
    fi

    echo "將建立 systemd 服務：${SERVICE_NAME}"
    echo "此操作需要 sudo 權限。"

    sudo bash -c "cat > '${SERVICE_FILE}' << EOF
[Unit]
Description=Chao Bi Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/.venv/bin/python3 -u chao_bi.py
Restart=on-failure
User=${USER}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF"

    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}"
    sudo systemctl start "${SERVICE_NAME}"

    echo "已建立並啟動 systemd 服務：${SERVICE_NAME}"
    echo "之後可使用："
    echo "  sudo systemctl status ${SERVICE_NAME}"
    echo "  sudo systemctl stop ${SERVICE_NAME}"
}

# 移除 systemd 服務（給 delete 用）
remove_systemd_service() {
    if ! command -v systemctl >/dev/null 2>&1; then
        return
    fi

    if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
        echo "正在移除 systemd 服務：${SERVICE_NAME} ..."
        sudo systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
        sudo systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
        sudo rm -f "${SERVICE_FILE}"
        sudo systemctl daemon-reload
        echo "systemd 服務已移除。"
    fi
}

# 處理 delete 指令
if [ "$1" = "delete" ]; then
    echo "確定要刪除所有檔案與設定嗎？此操作無法復原！(y/n)"
    read -r confirm
    if [ "$confirm" != "y" ]; then
        echo "已取消刪除操作。"
        exit 0
    fi

    echo "正在停止機器人 (process) ..."
    pkill -f "python3 -u chao_bi.py" 2>/dev/null || true

    echo "正在移除 systemd 服務（如果有設定）..."
    remove_systemd_service

    echo "正在刪除虛擬環境..."
    rm -rf "${SCRIPT_DIR}/.venv"

    echo "是否要清除 ~/.secret 內的金鑰檔案？(y/n)"
    read -r clear_secret
    if [ "$clear_secret" != "y" ]; then
        echo "已跳過 ~/.secret 清理。"
    else
        echo "正在檢查並清理 ~/.secret ..."
        if [ -d "${SECRET_DIR}" ]; then
            # 使用 bash 的 nullglob + dotglob 收集所有實際檔案（不含 . ..）
            shopt -s nullglob dotglob
            secret_entries=("${SECRET_DIR}"/*)
            shopt -u nullglob dotglob

            only_our_files=true
            for path in "${secret_entries[@]}"; do
                # 如果裡面有不是我們兩個檔案的東西，就標記為 false
                if [ "$path" != "$TELEGRAM_FILE" ] && [ "$path" != "$BINANCE_FILE" ]; then
                    only_our_files=false
                    break
                fi
            done

            # 先刪我們自己的檔案
            rm -f "$TELEGRAM_FILE" "$BINANCE_FILE"

            if [ "$only_our_files" = true ]; then
                echo "偵測到 ~/.secret 內只有本服務的檔案，將整個資料夾一併刪除。"
                rmdir "${SECRET_DIR}" 2>/dev/null || true
            else
                echo "~/.secret 中有其他檔案，僅刪除本服務使用的金鑰檔。"
            fi
        else
            echo "未發現 ~/.secret，略過。"
        fi
    fi

    echo "正在刪除此專案資料夾（包括 start.sh 自身）..."
    rm -rf "${SCRIPT_DIR}"

    echo "刪除完成。"
    exit 0
fi

# init 初始化模式
if [ "$1" = "init" ]; then
    echo "====== 初始化 Chao_Bi ======"
    cd "${SCRIPT_DIR}" || exit 1

    echo "檢查是否已經下載必要的原始碼..."

    # 檢查目前資料夾底下有沒有 chao_bi.py（代表已經有原始碼）
    if [ ! -f "${SCRIPT_DIR}/chao_bi.py" ]; then
        echo "未找到 chao_bi.py，將從 GitHub 下載 AChao0212/chao_bi 專案..."

        # 先 clone 到暫存資料夾，避免直接覆蓋目前檔案
        TMP_DIR="${SCRIPT_DIR}/.chao_bi_tmp_clone"
        rm -rf "${TMP_DIR}"
        git clone https://github.com/AChao0212/chao_bi.git "${TMP_DIR}"

        # 把內容搬到目前資料夾
        mv "${TMP_DIR}/"* "${SCRIPT_DIR}/"
        mv "${TMP_DIR}"/.[!.]* "${SCRIPT_DIR}/" 2>/dev/null || true
        rm -rf "${TMP_DIR}"

        echo "原始碼下載完成。"
    else
        # 如果有 .git，就順便幫忙 git pull 一下
        if [ -d "${SCRIPT_DIR}/.git" ]; then
            echo "偵測到 Git 專案，嘗試更新 (git pull)..."
            git -C "${SCRIPT_DIR}" pull --ff-only || echo "git pull 失敗，略過更新。"
        else
            echo "已找到原始碼，略過下載。"
        fi
    fi

    echo "建立虛擬環境..."
    python3 -m venv "${SCRIPT_DIR}/.venv"
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/.venv/bin/activate"

    echo "安裝所需套件..."
    pip install --upgrade pip
    pip install -r requirements.txt

    echo "建立 ~/.secret ..."
    mkdir -p "${SECRET_DIR}"
    touch "${TELEGRAM_FILE}"
    touch "${BINANCE_FILE}"

    echo "檢查 API KEY 設定狀態..."

    if [ ! -s "${TELEGRAM_FILE}" ] || [ ! -s "${BINANCE_FILE}" ]; then
        echo "尚未完成 API KEY 設定。"
        echo "請將您的 Telegram 與 Binance API 金鑰分別填入："
        echo "  ${TELEGRAM_FILE}"
        echo "  ${BINANCE_FILE}"
        echo "填寫完成後，請再次執行： ./start.sh init"
        exit 1
    fi

    echo "已偵測到 API KEY，接下來將啟動 Telegram 首次登入流程..."
    python3 login_once.py

    echo "您是否要將此服務設定為『開機自動啟動』（systemd）？(y/n)"
    read -r confirm
    if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
        create_systemd_service
        echo "初始化完成！之後可使用 systemd 管理服務，例如："
        echo "  sudo systemctl restart ${SERVICE_NAME}"
    else
        echo "已跳過 systemd 設定。"
        echo "初始化完成！之後請使用： ./start.sh 啟動機器人。"
    fi

    exit 0
fi

# update：更新程式碼與套件
if [ "$1" = "update" ]; then
    echo "====== 更新 Chao_Bi ======"
    cd "${SCRIPT_DIR}" || exit 1

    if [ -d "${SCRIPT_DIR}/.git" ]; then
        echo "正在從 Git 取得最新程式碼 (git pull)..."
        git pull --ff-only || {
            echo "git pull 失敗，請手動檢查衝突。"
            exit 1
        }
    else
        echo "目前資料夾不是 Git 專案，無法自動更新程式碼。"
    fi

    if [ -d "${SCRIPT_DIR}/.venv" ]; then
        echo "重新整理虛擬環境套件..."
        # shellcheck disable=SC1091
        source "${SCRIPT_DIR}/.venv/bin/activate"
        pip install --upgrade pip
        pip install -r requirements.txt
    else
        echo "尚未建立虛擬環境，請先執行： ./start.sh init"
        exit 1
    fi

    echo "更新完成。"
    echo "如果你有用 systemd，建議重新啟動服務："
    echo "  sudo systemctl restart ${SERVICE_NAME}"
    echo "如果是用 ./start.sh 啟動的，請先 ./start.sh stop 再 ./start.sh"
    exit 0
fi

# stop：單純殺掉目前的 python 行程
if [ "$1" = "stop" ]; then
    echo "正在停止機器人 (process)..."
    pkill -f "python3 -u chao_bi.py" 2>/dev/null || true
    echo "機器人已停止（如果原本是用 systemd 啟動，請改用： sudo systemctl stop ${SERVICE_NAME}）"
    exit 0
fi

# 日常啟動模式
if [ -z "$1" ]; then
    cd "${SCRIPT_DIR}" || exit 1

    if [ ! -d "${SCRIPT_DIR}/.venv" ]; then
        echo "尚未初始化！請先執行： ./start.sh init"
        exit 1
    fi

    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/.venv/bin/activate"

    if [ ! -f "${SCRIPT_DIR}/chao_bi.session" ]; then
        echo "偵測到尚未登入 Telegram，請先執行： ./start.sh init"
        exit 1
    fi

    if pgrep -f "python3 -u chao_bi.py" >/dev/null; then
        echo "機器人已在運行中，無需重複啟動。"
        exit 0
    fi

    python3 -u chao_bi.py > log.txt 2>&1 &
    echo "機器人已啟動，日誌輸出至 log.txt"
    echo "如果你有設定 systemd，以後建議用 systemd 管理啟動/停止："
    echo "  sudo systemctl restart ${SERVICE_NAME}"
    exit 0
fi

# 若參數無效 → 顯示用法
print_usage
exit 0