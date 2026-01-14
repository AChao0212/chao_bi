#!/bin/bash

# =============================================================================
# Chao Bi 交易機器人 - 啟動/管理腳本
# 支援 Linux (Ubuntu) 與 macOS
# =============================================================================

# 專案基本設定
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="chao_bi"
SERVICE_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
LOG_FILE="${SCRIPT_DIR}/log.txt"
PID_FILE="${SCRIPT_DIR}/.chao_bi.pid"

SECRET_DIR="$HOME/.secret"
TELEGRAM_FILE="${SECRET_DIR}/telegram.txt"
BINANCE_FILE="${SECRET_DIR}/binance.txt"

# 偵測作業系統
detect_os() {
    case "$(uname -s)" in
        Linux*)     OS_TYPE="linux";;
        Darwin*)    OS_TYPE="macos";;
        *)          OS_TYPE="unknown";;
    esac
}
detect_os

# 顏色輸出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_info() { echo -e "${BLUE}[INFO]${NC} $1"; }
print_success() { echo -e "${GREEN}[OK]${NC} $1"; }
print_warning() { echo -e "${YELLOW}[WARN]${NC} $1"; }
print_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 使用說明函式
print_usage() {
    echo ""
    echo "Chao Bi 交易機器人 - 管理腳本"
    echo ""
    echo "用法："
    echo "  ./start.sh            - 啟動機器人（背景執行）"
    echo "  ./start.sh init       - 初始化：下載/更新原始碼 + 建立環境 + 安裝套件 + Telegram 登入"
    echo "  ./start.sh stop       - 停止機器人"
    echo "  ./start.sh restart    - 重新啟動機器人"
    echo "  ./start.sh status     - 查看機器人執行狀態"
    echo "  ./start.sh logs       - 查看即時日誌 (Ctrl+C 退出)"
    echo "  ./start.sh update     - 更新程式碼與套件"
    echo "  ./start.sh delete     - 停止並刪除所有檔案、設定與服務"
    echo ""
    echo "系統資訊："
    echo "  作業系統: ${OS_TYPE}"
    echo "  專案目錄: ${SCRIPT_DIR}"
    echo ""
}

# 取得正在執行的 PID
get_running_pid() {
    if [ -f "$PID_FILE" ]; then
        local pid
        pid=$(cat "$PID_FILE")
        if ps -p "$pid" > /dev/null 2>&1; then
            echo "$pid"
            return 0
        fi
    fi
    # 備用方法：用 pgrep 搜尋
    pgrep -f "python3.*chao_bi.py" 2>/dev/null | head -1
}

# 檢查是否正在執行
is_running() {
    local pid
    pid=$(get_running_pid)
    [ -n "$pid" ]
}

# 建立 systemd 服務 (僅 Linux)
create_systemd_service() {
    if [ "$OS_TYPE" != "linux" ]; then
        print_warning "systemd 僅支援 Linux，macOS 請使用 ./start.sh 手動啟動"
        return
    fi

    if ! command -v systemctl >/dev/null 2>&1; then
        print_warning "系統似乎沒有 systemd（找不到 systemctl），無法建立服務。"
        return
    fi

    print_info "將建立 systemd 服務：${SERVICE_NAME}"
    echo "此操作需要 sudo 權限。"

    sudo bash -c "cat > '${SERVICE_FILE}' << EOF
[Unit]
Description=Chao Bi Telegram Trading Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=${SCRIPT_DIR}
ExecStart=${SCRIPT_DIR}/.venv/bin/python3 -u chao_bi.py
Restart=on-failure
RestartSec=10
User=${USER}
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF"

    sudo systemctl daemon-reload
    sudo systemctl enable "${SERVICE_NAME}"
    sudo systemctl start "${SERVICE_NAME}"

    print_success "已建立並啟動 systemd 服務：${SERVICE_NAME}"
    echo ""
    echo "之後可使用："
    echo "  sudo systemctl status ${SERVICE_NAME}"
    echo "  sudo systemctl stop ${SERVICE_NAME}"
    echo "  sudo systemctl restart ${SERVICE_NAME}"
    echo "  sudo journalctl -u ${SERVICE_NAME} -f"
}

# 移除 systemd 服務
remove_systemd_service() {
    if [ "$OS_TYPE" != "linux" ]; then
        return
    fi

    if ! command -v systemctl >/dev/null 2>&1; then
        return
    fi

    if systemctl list-unit-files | grep -q "^${SERVICE_NAME}.service"; then
        print_info "正在移除 systemd 服務：${SERVICE_NAME} ..."
        sudo systemctl stop "${SERVICE_NAME}" 2>/dev/null || true
        sudo systemctl disable "${SERVICE_NAME}" 2>/dev/null || true
        sudo rm -f "${SERVICE_FILE}"
        sudo systemctl daemon-reload
        print_success "systemd 服務已移除。"
    fi
}

# 停止機器人
stop_bot() {
    local pid
    pid=$(get_running_pid)

    if [ -z "$pid" ]; then
        print_warning "機器人未在執行中"
        return 0
    fi

    print_info "正在停止機器人 (PID: $pid)..."
    kill "$pid" 2>/dev/null

    # 等待程序結束
    local count=0
    while ps -p "$pid" > /dev/null 2>&1 && [ $count -lt 10 ]; do
        sleep 1
        count=$((count + 1))
    done

    if ps -p "$pid" > /dev/null 2>&1; then
        print_warning "程序未正常結束，強制終止..."
        kill -9 "$pid" 2>/dev/null
    fi

    rm -f "$PID_FILE"
    print_success "機器人已停止"
}

# 啟動機器人
start_bot() {
    cd "${SCRIPT_DIR}" || exit 1

    if [ ! -d "${SCRIPT_DIR}/.venv" ]; then
        print_error "尚未初始化！請先執行： ./start.sh init"
        exit 1
    fi

    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/.venv/bin/activate"

    if [ ! -f "${SCRIPT_DIR}/chao_bi.session" ]; then
        print_error "偵測到尚未登入 Telegram，請先執行： ./start.sh init"
        exit 1
    fi

    if is_running; then
        print_warning "機器人已在執行中 (PID: $(get_running_pid))"
        exit 0
    fi

    print_info "正在啟動機器人..."
    nohup python3 -u chao_bi.py > "$LOG_FILE" 2>&1 &
    local pid=$!
    echo "$pid" > "$PID_FILE"

    sleep 2

    if ps -p "$pid" > /dev/null 2>&1; then
        print_success "機器人已啟動 (PID: $pid)"
        echo "日誌輸出至: $LOG_FILE"
        echo ""
        echo "常用指令："
        echo "  ./start.sh logs    - 查看即時日誌"
        echo "  ./start.sh status  - 查看執行狀態"
        echo "  ./start.sh stop    - 停止機器人"
    else
        print_error "啟動失敗，請查看日誌: $LOG_FILE"
        rm -f "$PID_FILE"
        exit 1
    fi
}

# 顯示狀態
show_status() {
    echo ""
    echo "=== Chao Bi 機器人狀態 ==="
    echo ""

    local pid
    pid=$(get_running_pid)

    if [ -n "$pid" ]; then
        print_success "執行中 (PID: $pid)"

        # 顯示執行時間
        if [ "$OS_TYPE" = "linux" ]; then
            local start_time
            start_time=$(ps -o lstart= -p "$pid" 2>/dev/null)
            [ -n "$start_time" ] && echo "啟動時間: $start_time"
        elif [ "$OS_TYPE" = "macos" ]; then
            local start_time
            start_time=$(ps -o lstart= -p "$pid" 2>/dev/null)
            [ -n "$start_time" ] && echo "啟動時間: $start_time"
        fi

        # 顯示記憶體使用
        if [ "$OS_TYPE" = "linux" ]; then
            local mem
            mem=$(ps -o rss= -p "$pid" 2>/dev/null)
            [ -n "$mem" ] && echo "記憶體使用: $((mem / 1024)) MB"
        elif [ "$OS_TYPE" = "macos" ]; then
            local mem
            mem=$(ps -o rss= -p "$pid" 2>/dev/null)
            [ -n "$mem" ] && echo "記憶體使用: $((mem / 1024)) MB"
        fi
    else
        print_warning "未在執行中"
    fi

    echo ""

    # 顯示交易狀態
    if [ -f "${SCRIPT_DIR}/chao_bi_state.json" ]; then
        local trade_count
        trade_count=$(grep -o '"entry_order_id"' "${SCRIPT_DIR}/chao_bi_state.json" 2>/dev/null | wc -l | tr -d ' ')
        echo "追蹤中的交易: $trade_count 筆"
    fi

    # 顯示交易紀錄數
    if [ -f "${SCRIPT_DIR}/trade_log.csv" ]; then
        local log_count
        log_count=$(($(wc -l < "${SCRIPT_DIR}/trade_log.csv" | tr -d ' ') - 1))
        [ $log_count -lt 0 ] && log_count=0
        echo "已完成交易紀錄: $log_count 筆"
    fi

    # 顯示日誌檔案大小
    if [ -f "$LOG_FILE" ]; then
        local log_size
        if [ "$OS_TYPE" = "macos" ]; then
            log_size=$(stat -f%z "$LOG_FILE" 2>/dev/null)
        else
            log_size=$(stat -c%s "$LOG_FILE" 2>/dev/null)
        fi
        if [ -n "$log_size" ]; then
            echo "日誌檔案大小: $((log_size / 1024)) KB"
        fi
    fi

    echo ""
}

# 查看日誌
show_logs() {
    if [ ! -f "$LOG_FILE" ]; then
        print_warning "日誌檔案不存在: $LOG_FILE"
        exit 1
    fi

    print_info "顯示即時日誌 (按 Ctrl+C 退出)..."
    echo ""
    tail -f "$LOG_FILE"
}

# =============================================================================
# 主要指令處理
# =============================================================================

# delete 指令
if [ "$1" = "delete" ]; then
    echo ""
    print_warning "確定要刪除所有檔案與設定嗎？此操作無法復原！"
    echo -n "輸入 'yes' 確認刪除: "
    read -r confirm
    if [ "$confirm" != "yes" ]; then
        print_info "已取消刪除操作。"
        exit 0
    fi

    print_info "正在停止機器人..."
    stop_bot

    print_info "正在移除 systemd 服務（如果有設定）..."
    remove_systemd_service

    print_info "正在刪除虛擬環境..."
    rm -rf "${SCRIPT_DIR}/.venv"

    echo ""
    echo -n "是否要清除 ~/.secret 內的金鑰檔案？(y/n): "
    read -r clear_secret
    if [ "$clear_secret" != "y" ]; then
        print_info "已跳過 ~/.secret 清理。"
    else
        print_info "正在檢查並清理 ~/.secret ..."
        if [ -d "${SECRET_DIR}" ]; then
            shopt -s nullglob dotglob
            secret_entries=("${SECRET_DIR}"/*)
            shopt -u nullglob dotglob

            only_our_files=true
            for path in "${secret_entries[@]}"; do
                if [ "$path" != "$TELEGRAM_FILE" ] && [ "$path" != "$BINANCE_FILE" ]; then
                    only_our_files=false
                    break
                fi
            done

            rm -f "$TELEGRAM_FILE" "$BINANCE_FILE"

            if [ "$only_our_files" = true ]; then
                print_info "偵測到 ~/.secret 內只有本服務的檔案，將整個資料夾一併刪除。"
                rmdir "${SECRET_DIR}" 2>/dev/null || true
            else
                print_info "~/.secret 中有其他檔案，僅刪除本服務使用的金鑰檔。"
            fi
        else
            print_info "未發現 ~/.secret，略過。"
        fi
    fi

    print_info "正在刪除此專案資料夾..."
    rm -rf "${SCRIPT_DIR}"

    print_success "刪除完成。"
    exit 0
fi

# init 初始化
if [ "$1" = "init" ]; then
    echo ""
    echo "====== 初始化 Chao Bi ======"
    echo ""
    cd "${SCRIPT_DIR}" || exit 1

    print_info "檢查是否已經下載必要的原始碼..."

    if [ ! -f "${SCRIPT_DIR}/chao_bi.py" ]; then
        print_info "未找到 chao_bi.py，將從 GitHub 下載..."

        TMP_DIR="${SCRIPT_DIR}/.chao_bi_tmp_clone"
        rm -rf "${TMP_DIR}"
        git clone https://github.com/AChao0212/chao_bi.git "${TMP_DIR}"

        mv "${TMP_DIR}/"* "${SCRIPT_DIR}/"
        mv "${TMP_DIR}"/.[!.]* "${SCRIPT_DIR}/" 2>/dev/null || true
        rm -rf "${TMP_DIR}"

        print_success "原始碼下載完成。"
    else
        if [ -d "${SCRIPT_DIR}/.git" ]; then
            print_info "偵測到 Git 專案，嘗試更新 (git pull)..."
            git -C "${SCRIPT_DIR}" pull --ff-only || print_warning "git pull 失敗，略過更新。"
        else
            print_info "已找到原始碼，略過下載。"
        fi
    fi

    print_info "建立虛擬環境..."
    python3 -m venv "${SCRIPT_DIR}/.venv"
    # shellcheck disable=SC1091
    source "${SCRIPT_DIR}/.venv/bin/activate"

    print_info "安裝所需套件..."
    pip install --upgrade pip
    pip install -r requirements.txt

    print_info "建立 ~/.secret ..."
    mkdir -p "${SECRET_DIR}"
    touch "${TELEGRAM_FILE}"
    touch "${BINANCE_FILE}"

    print_info "檢查 API KEY 設定狀態..."

    if [ ! -s "${TELEGRAM_FILE}" ] || [ ! -s "${BINANCE_FILE}" ]; then
        echo ""
        print_warning "尚未完成 API KEY 設定。"
        echo ""
        echo "請將您的 Telegram 與 Binance API 金鑰分別填入："
        echo "  ${TELEGRAM_FILE}"
        echo "  ${BINANCE_FILE}"
        echo ""
        echo "填寫完成後，請再次執行： ./start.sh init"
        exit 1
    fi

    print_success "已偵測到 API KEY"
    print_info "接下來將啟動 Telegram 首次登入流程..."
    python3 login_once.py

    echo ""
    if [ "$OS_TYPE" = "linux" ]; then
        echo -n "您是否要將此服務設定為『開機自動啟動』（systemd）？(y/n): "
        read -r confirm
        if [ "$confirm" = "y" ] || [ "$confirm" = "Y" ]; then
            create_systemd_service
            echo ""
            print_success "初始化完成！之後可使用 systemd 管理服務。"
        else
            print_info "已跳過 systemd 設定。"
            echo ""
            print_success "初始化完成！之後請使用： ./start.sh 啟動機器人。"
        fi
    else
        print_info "macOS 不支援 systemd，請使用 ./start.sh 手動啟動。"
        echo ""
        print_success "初始化完成！請使用： ./start.sh 啟動機器人。"
    fi

    exit 0
fi

# update 更新
if [ "$1" = "update" ]; then
    echo ""
    echo "====== 更新 Chao Bi ======"
    echo ""
    cd "${SCRIPT_DIR}" || exit 1

    if [ -d "${SCRIPT_DIR}/.git" ]; then
        print_info "正在從 Git 取得最新程式碼..."
        git pull --ff-only || {
            print_error "git pull 失敗，請手動檢查衝突。"
            exit 1
        }
    else
        print_warning "目前資料夾不是 Git 專案，無法自動更新程式碼。"
    fi

    if [ -d "${SCRIPT_DIR}/.venv" ]; then
        print_info "重新整理虛擬環境套件..."
        # shellcheck disable=SC1091
        source "${SCRIPT_DIR}/.venv/bin/activate"
        pip install --upgrade pip
        pip install -r requirements.txt
    else
        print_error "尚未建立虛擬環境，請先執行： ./start.sh init"
        exit 1
    fi

    print_success "更新完成。"
    echo ""
    echo "如果機器人正在執行，建議重新啟動："
    echo "  ./start.sh restart"
    exit 0
fi

# stop 停止
if [ "$1" = "stop" ]; then
    stop_bot
    exit 0
fi

# restart 重啟
if [ "$1" = "restart" ]; then
    stop_bot
    sleep 2
    start_bot
    exit 0
fi

# status 狀態
if [ "$1" = "status" ]; then
    show_status
    exit 0
fi

# logs 日誌
if [ "$1" = "logs" ]; then
    show_logs
    exit 0
fi

# 無參數 = 啟動
if [ -z "$1" ]; then
    start_bot
    exit 0
fi

# 無效參數
print_error "未知的指令: $1"
print_usage
exit 1
