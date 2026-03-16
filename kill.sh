#!/bin/bash

PID_FILE="./logs/last_run_pid"

kill_pid_or_group() {
    local pid="$1"
    if [ -z "$pid" ]; then
        return
    fi

    if ! kill -0 "$pid" 2>/dev/null; then
        return
    fi

    pgid=$(ps -o pgid= -p "$pid" 2>/dev/null | tr -d ' ')
    if [ -n "$pgid" ]; then
        kill -TERM -- "-$pgid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null
    else
        kill -TERM "$pid" 2>/dev/null
    fi

    sleep 1

    if kill -0 "$pid" 2>/dev/null; then
        if [ -n "$pgid" ]; then
            kill -KILL -- "-$pgid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
        else
            kill -KILL "$pid" 2>/dev/null
        fi
        sleep 0.2
    fi
}

find_pids_by_port() {
    local port="$1"
    if [ -z "$port" ]; then
        return
    fi

    if command -v lsof >/dev/null 2>&1; then
        lsof -nP -t -iTCP:"$port" -sTCP:LISTEN 2>/dev/null | sort -u
        return
    fi

    if command -v ss >/dev/null 2>&1; then
        ss -lptn "sport = :$port" 2>/dev/null | awk -F'pid=' 'NR>1 {print $2}' | awk -F',' '{print $1}' | sort -u
        return
    fi

    if command -v netstat >/dev/null 2>&1; then
        netstat -lntp 2>/dev/null | awk -v p=":$port" '$4 ~ p {print $7}' | awk -F'/' '{print $1}' | sort -u
        return
    fi
}

echo "🔍 Killing processes by ports (9897/9898)... | 按端口终止进程..."

for port in 9897 9898; do
    pids=$(find_pids_by_port "$port")
    if [ -n "$pids" ]; then
        for pid in $pids; do
            if [ "$pid" = "$$" ]; then
                continue
            fi
            echo "🔪 Killing port $port (PID: $pid)..."
            kill_pid_or_group "$pid"
        done
    else
        echo "ℹ️  No listener found on port $port."
    fi
done

if [ ! -f "$PID_FILE" ]; then
    echo "⚠️  PID file '$PID_FILE' not found. Skip PID cleanup. | 未找到 PID 文件，跳过 PID 清理。"
    echo "---------------------------------------------------"
    echo "🛑 Done. | 已完成。"
    echo "---------------------------------------------------"
    exit 0
fi

echo "🔍 Reading PIDs from $PID_FILE... | 正在读取 PID..."

# ==========================================
# 2. Read PIDs and Kill / 读取并终止进程
# ==========================================
# Read the file line by line
while IFS='=' read -r key pid; do
    if [ -z "$pid" ]; then
        continue
    fi

    if kill -0 "$pid" 2>/dev/null; then
        echo "🔪 Killing $key (PID: $pid)... | 正在终止 $key..."
        kill_pid_or_group "$pid"

        if kill -0 "$pid" 2>/dev/null; then
            echo "❌ Process $pid still running. | 进程 $pid 仍在运行。"
        else
            echo "✅ Process $pid stopped. | 进程 $pid 已停止。"
        fi
    else
        echo "⚠️  Process $key (PID: $pid) not found (already stopped). | 进程 $pid 未找到 (可能已停止)。"
    fi

done < "$PID_FILE"

# ==========================================
# 3. Cleanup / 清理文件
# ==========================================
rm "$PID_FILE"
echo "🗑️  Removed PID file: $PID_FILE | 已删除 PID 文件。"

echo "---------------------------------------------------"
echo "🛑 All services stopped. | 所有服务已停止。"
echo "---------------------------------------------------"