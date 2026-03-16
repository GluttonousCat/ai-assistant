#!/bin/bash

# ==========================================
# Configuration / 配置
# ==========================================
# Define log file names / 定义日志文件名
WS_OUT="logs/websocket.out"
WS_ERR="logs/websocket.err"
KWS_OUT="logs/kws.out"
KWS_ERR="logs/kws.err"
PID_FILE="logs/last_run_pid"

# ==========================================
# 0. Ensure Bash / 确保使用 Bash 运行
# ==========================================
if [ -z "$BASH_VERSION" ]; then
    echo "⚠️  Warning: Script running with 'sh', switching to 'bash'... | 警告: 正切换到 bash 运行..."
    exec bash "$0" "$@"
fi

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR" || exit 1

mkdir -p "logs"

STARTUP_LOG="logs/startup.log"
exec > >(tee -a "$STARTUP_LOG") 2>&1

ts() { date "+%Y-%m-%d %H:%M:%S"; }
log() { echo "[$(ts)] $*"; }

trap 'rc=$?; log "start.sh exit rc=${rc}"; exit $rc' EXIT

log "Start script: $0"
log "PWD=$(pwd)"
log "USER=$(id -un) UID=$(id -u)"
log "SHELL=$SHELL"
log "PATH=$PATH"
env | sort > logs/start.env 2>/dev/null || true
log "Wrote logs/start.env"

# ==========================================
# 1. Activate Virtual Environment / 激活虚拟环境
# ==========================================
VENV_PATH="./myvenv/bin/activate"
if [ -f "$VENV_PATH" ]; then
    . "$VENV_PATH"
    echo "✅ Virtual environment activated: $(which python) | 虚拟环境已激活"
    python -V || true
else
    echo "❌ Error: Virtual environment not found at $VENV_PATH | 错误: 找不到虚拟环境"
    exit 1
fi

# ==========================================
# 1.5 Ensure audio runtime (PulseAudio) / 确保音频运行时就绪
# ==========================================
UID_NUM=$(id -u)
RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$UID_NUM}"
export XDG_RUNTIME_DIR="$RUNTIME_DIR"
PULSE_SOCK="$RUNTIME_DIR/pulse/native"

if [ ! -S "$PULSE_SOCK" ]; then
    if command -v pulseaudio >/dev/null 2>&1; then
        pulseaudio --start >/dev/null 2>&1 || true
    fi
fi

WAIT_PULSE_MAX=30
i=1
while [ "$i" -le "$WAIT_PULSE_MAX" ]; do
    if [ -S "$PULSE_SOCK" ]; then
        export PULSE_SERVER="unix:$PULSE_SOCK"
        echo "✅ PulseAudio ready: $PULSE_SOCK"
        break
    fi
    sleep 1
    i=$((i + 1))
done

if [ ! -S "$PULSE_SOCK" ]; then
    echo "⚠️  PulseAudio socket not ready after ${WAIT_PULSE_MAX}s: $PULSE_SOCK"
fi

if command -v pactl >/dev/null 2>&1; then
    {
        echo "==== pactl info ===="
        pactl info || true
        echo "==== pactl list short sources ===="
        pactl list short sources || true
        echo "==== pactl list short sinks ===="
        pactl list short sinks || true
    } > ./logs/pulse.info 2>&1 || true
    log "Wrote ./logs/pulse.info"
else
    log "pactl not found"
fi

# ==========================================
# 2. Find the target file / 扫描目标服务端文件
# ==========================================
TARGET_SERVER_FILE="server/main.py"

if [ ! -f "$TARGET_SERVER_FILE" ]; then
    echo "❌ Error: '$TARGET_SERVER_FILE' not found | 错误：文件不存在"
    exit 1
fi

echo "🔍 Found latest server file: $TARGET_SERVER_FILE | 找到最新的服务端文件"

# ==========================================
# 3. Run Websocket Server / 运行 Websocket 服务
# ==========================================
echo "🚀 Starting Websocket Server... | 正在启动 Websocket Server..."
nohup python "$TARGET_SERVER_FILE" > "$WS_OUT" 2> "$WS_ERR" &
WS_PID=$!
echo "   Websocket Server PID: $WS_PID"
log "Websocket cmd: python $TARGET_SERVER_FILE"
ps -o pid,ppid,pgid,cmd -p "$WS_PID" 2>/dev/null || true

# ==========================================
# 4. Port Check / 端口检测
# ==========================================
MAX_RETRIES=240
SLEEP_TIME=0.5

check_port() {
    local port=$1
    (echo > /dev/tcp/127.0.0.1/$port) >/dev/null 2>&1
    return $?
}

echo "⏳ Waiting for ports 9897 & 9898 (Timeout 120s)... | 等待端口就绪..."

ports_ready=false
i=1
while [ "$i" -le "$MAX_RETRIES" ]; do
    if ! kill -0 "$WS_PID" 2>/dev/null; then
        echo "❌ Error: Websocket server exited early (PID $WS_PID). | 错误: Websocket 服务进程提前退出。"
        echo "   Please check $WS_ERR for details. | 请检查错误日志。"
        if [ -f "$WS_ERR" ]; then
            echo "---- tail $WS_ERR ----"
            tail -n 80 "$WS_ERR" || true
            echo "---- end tail ----"
        fi
        exit 1
    fi
    if [ $((i % 10)) -eq 0 ]; then
        log "Waiting ports... attempt=$i/${MAX_RETRIES} (sleep=${SLEEP_TIME}s)"
    fi
    if check_port 9897 && check_port 9898; then
        ports_ready=true
        break
    fi
    sleep $SLEEP_TIME
    i=$((i + 1))
done

if [ "$ports_ready" = true ]; then
    echo "✅ Ports 9897 and 9898 are ready. | 端口检测正常。"
else
    echo "❌ Error: Timeout waiting for ports to open. | 错误: 等待端口启动超时。"
    echo "   Please check $WS_ERR for details. | 请检查错误日志。"
    if [ -f "$WS_ERR" ]; then
        echo "---- tail $WS_ERR ----"
        tail -n 80 "$WS_ERR" || true
        echo "---- end tail ----"
    fi
    exit 1
fi

# ==========================================
# 5. Run kws.sh / 运行 kws.sh
# ==========================================
# (Model selection removed as requested / 已移除模型选择步骤)

KWS_SCRIPT="./kws.sh"

if [ -f "$KWS_SCRIPT" ]; then
    echo "🚀 Starting kws.sh..."
    
    # Ensure it's executable / 确保有执行权限
    chmod +x "$KWS_SCRIPT"

    # Run the script directly without arguments
    # 直接运行脚本，不带参数
    nohup "$KWS_SCRIPT" > "$KWS_OUT" 2> "$KWS_ERR" &
    
    KWS_PID=$!
    echo "   KWS Process PID: $KWS_PID"
else
    echo "❌ Error: '$KWS_SCRIPT' not found. | 错误: 找不到 $KWS_SCRIPT"
    # Optional: Kill websocket server if kws fails to start
    # kill $WS_PID
    exit 1
fi

# ==========================================
# 6. Start KWS Monitor / 启动 KWS 监控
# ==========================================
MONITOR_SCRIPT="./kws_monitor.sh"

if [ -f "$MONITOR_SCRIPT" ]; then
    chmod +x "$MONITOR_SCRIPT"
    nohup bash "$MONITOR_SCRIPT" > "./logs/kws_monitor.out" 2> "./logs/kws_monitor.err" &
    MONITOR_PID=$!
    echo "   KWS Monitor PID: $MONITOR_PID"
else
    echo "⚠️  Monitor script not found, skipping..."
    MONITOR_PID=""
fi

# ==========================================
# 7. Save PIDs and Finish / 记录 PID 并结束
# ==========================================
echo "WS_PID=$WS_PID" > "$PID_FILE"
echo "KWS_PID=$KWS_PID" >> "$PID_FILE"
if [ -n "$MONITOR_PID" ]; then
    echo "MONITOR_PID=$MONITOR_PID" >> "$PID_FILE"
fi

echo "---------------------------------------------------"
echo "🎉 All services started! | 所有服务已启动!"
echo "📄 Server Logs: $WS_OUT / $WS_ERR"
echo "📄 KWS Logs:    $KWS_OUT / $KWS_ERR"
echo "📄 Monitor Logs: ./logs/kws_monitor.out / ./logs/kws_monitor.err"
echo "🔢 PIDs saved to: $PID_FILE"
echo "---------------------------------------------------"
