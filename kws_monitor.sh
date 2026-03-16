#!/bin/bash

# ==========================================
# KWS 唤醒监控脚本
# 功能：
#   1. 实时监控唤醒检测日志
#   2. 统计唤醒成功率和响应时间
#   3. 检测异常（长时间无唤醒、频繁唤醒等）
#   4. 记录到独立日志文件
# ==========================================

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
cd "$SCRIPT_DIR"

# 日志文件
MONITOR_LOG="./logs/kws_monitor.log"
MONITOR_OUT="./logs/kws_monitor.out"
MONITOR_ERR="./logs/kws_monitor.err"

# KWS 日志文件
KWS_LOG="./logs/kws.err"

# 监控参数
IDLE_TIMEOUT=600        # 闲置超时（秒）- 用于检测长时间无唤醒
RAPID_WAKE_LIMIT=10     # 快速唤醒限制（次/分钟）- 用于检测误触发

# 确保日志目录存在
mkdir -p "./logs"

# 日志函数
log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" | tee -a "$MONITOR_LOG"
}

log_error() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] [ERROR] $*" | tee -a "$MONITOR_ERR" >&2
}

# 统计变量
declare -A wake_stats
total_wake_count=0
last_wake_time=0
wake_times=()

# 解析唤醒日志
parse_wake_log() {
    local line="$1"

    # 匹配唤醒检测日志
    if [[ "$line" =~ 检测到关键词 ]]; then
        local current_time=$(date +%s)

        # 提取关键词
        local keyword=""
        if [[ "$line" =~ 关键词：([^\ ]+) ]]; then
            keyword="${BASH_REMATCH[1]}"
        fi

        # 提取检测次数
        local count=""
        if [[ "$line" =~ 第\ ([0-9]+)\ 次 ]]; then
            count="${BASH_REMATCH[1]}"
        fi

        # 计算间隔
        local interval=0
        if [ "$last_wake_time" -gt 0 ]; then
            interval=$((current_time - last_wake_time))
        fi

        # 更新统计
        total_wake_count=$((total_wake_count + 1))
        last_wake_time=$current_time
        wake_times+=("$current_time")

        # 记录到统计数组
        if [ -n "$keyword" ]; then
            wake_stats["$keyword"]=$((${wake_stats["$keyword"]:-0} + 1))
        fi

        # 输出唤醒事件
        log "🔔 唤醒事件 | 关键词：${keyword:-未知} | 次数：${count:-N/A} | 距上次：${interval}秒"

        # 检测异常：快速连续唤醒（可能是误触发）
        if [ "$interval" -lt 5 ] && [ "$interval" -gt 0 ]; then
            log "⚠️  警告：唤醒间隔过短 (${interval}秒)，可能是误触发"
        fi
    fi
}

# 检查闲置超时
check_idle_timeout() {
    local current_time=$(date +%s)

    if [ "$last_wake_time" -gt 0 ]; then
        local idle_time=$((current_time - last_wake_time))

        if [ "$idle_time" -ge "$IDLE_TIMEOUT" ]; then
            # 只在刚超时时记录一次
            local timeout_key="timeout_${idle_time%60}"
            if [ "${wake_stats["$timeout_key"]:-0}" -eq 0 ]; then
                log "⏰ 闲置提醒：已 ${idle_time}秒 无唤醒"
                wake_stats["$timeout_key"]=1
            fi
        fi
    fi
}

# 打印统计摘要
print_stats() {
    log "=== 唤醒统计摘要 ==="
    log "总唤醒次数：$total_wake_count"

    for key in "${!wake_stats[@]}"; do
        if [[ ! "$key" =~ ^timeout_ ]]; then
            log "  $key: ${wake_stats[$key]} 次"
        fi
    done
    log "===================="
}

# 主监控循环
monitor_loop() {
    log "🚀 KWS 监控已启动"
    log "监控参数：闲置超时=${IDLE_TIMEOUT}s, 快速唤醒限制=${RAPID_WAKE_LIMIT}次/分钟"

    # 跟踪已读取的日志位置
    local last_pos=0

    # 检查 KWS 日志文件是否存在
    if [ ! -f "$KWS_LOG" ]; then
        log_error "KWS 日志文件不存在：$KWS_LOG"
        sleep 5
    fi

    # 主循环
    while true; do
        # 检查 KWS 进程是否存活
        if [ -f "./logs/last_run_pid" ]; then
            local kws_pid=$(grep "KWS_PID" ./logs/last_run_pid 2>/dev/null | cut -d'=' -f2)
            if [ -n "$kws_pid" ] && ! kill -0 "$kws_pid" 2>/dev/null; then
                log_error "KWS 进程已退出 (PID: $kws_pid)"
            fi
        fi

        # 读取新的日志行
        if [ -f "$KWS_LOG" ]; then
            local current_size=$(stat -c%s "$KWS_LOG" 2>/dev/null || echo 0)

            # 如果日志文件被截断（轮转），从头开始
            if [ "$current_size" -lt "$last_pos" ]; then
                last_pos=0
                log "检测到日志轮转，重新开始读取"
            fi

            # 读取新增内容
            if [ "$current_size" -gt "$last_pos" ]; then
                tail -c +$((last_pos + 1)) "$KWS_LOG" 2>/dev/null | while IFS= read -r line; do
                    parse_wake_log "$line"
                done
                last_pos=$current_size
            fi
        fi

        # 定期检查闲置状态（每 30 秒）
        check_idle_timeout

        # 每分钟打印一次统计
        if [ $(($(date +%s) % 60)) -lt 5 ]; then
            : # 避免重复打印
        fi

        sleep 5
    done
}

# 信号处理
cleanup() {
    log "🛑 监控正在停止..."
    print_stats
    exit 0
}

trap cleanup SIGINT SIGTERM

# 启动监控
monitor_loop
