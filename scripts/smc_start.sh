#!/usr/bin/env bash
# smc 服务启停脚本
# 用法: ./smc.sh {start|stop|restart|status|log}

set -u

APP_NAME="smc"
APP_USER="opinion"
APP_DIR="/www/wwwroot/smc"
PYTHON="$APP_DIR/.venv/bin/python"
ENTRY="run.py"
LOG_DIR="/data"
LOG_FILE="$LOG_DIR/logs/app.log"
PID_FILE="$APP_DIR/data/$APP_NAME.pid"
STOP_TIMEOUT=15   # 优雅停止等待秒数，超时 kill -9
START_CHECK=3     # 启动后等待几秒检查存活

# 用绝对路径解释器启动，命令行唯一，便于精确匹配进程
MATCH="^${PYTHON} ${ENTRY}"
CUR_USER="$(id -un)"

run_as() {
  if [ "$CUR_USER" = "$APP_USER" ]; then
    bash -c "$1"
  else
    su -s /bin/bash "$APP_USER" -c "$1"
  fi
}

check_user() {
  if [ "$CUR_USER" != "$APP_USER" ] && [ "$(id -u)" -ne 0 ]; then
    echo "请使用 root 或 $APP_USER 用户执行" >&2
    exit 1
  fi
}

# 日志目录在应用目录外，需保证 APP_USER 对日志文件有写权限
prepare_log() {
  if [ "$(id -u)" -eq 0 ]; then
    mkdir -p "$LOG_DIR"
    touch "$LOG_FILE"
    chown "$APP_USER": "$LOG_FILE"
  fi
  if ! run_as "touch '$LOG_FILE'" 2>/dev/null; then
    echo "[$APP_NAME] $APP_USER 无法写入 $LOG_FILE，请用 root 执行一次或手动授权" >&2
    return 1
  fi
}

get_pid() {
  pgrep -o -f "$MATCH" 2>/dev/null
}

start() {
  local pid
  pid="$(get_pid)"
  if [ -n "$pid" ]; then
    echo "[$APP_NAME] 已在运行 (PID $pid)"
    return 0
  fi
  if [ ! -x "$PYTHON" ]; then
    echo "[$APP_NAME] 找不到解释器: $PYTHON" >&2
    return 1
  fi

  prepare_log || return 1

  echo "[$APP_NAME] 启动中..."
  # setsid: 独立会话/进程组，stop 时可连同子进程一起停
  run_as "
    cd '$APP_DIR' || exit 1
    source .venv/bin/activate
    echo \"==== \$(date '+%F %T') start ====\" >> '$LOG_FILE'
    setsid nohup '$PYTHON' $ENTRY >> '$LOG_FILE' 2>&1 < /dev/null &
    echo \$! > '$PID_FILE'
  "

  sleep "$START_CHECK"
  pid="$(get_pid)"
  if [ -n "$pid" ]; then
    echo "[$APP_NAME] 启动成功 (PID $pid)"
  else
    echo "[$APP_NAME] 启动失败，最近日志：" >&2
    tail -n 30 "$LOG_FILE" >&2
    return 1
  fi
}

stop() {
  local pid i left
  pid="$(get_pid)"
  if [ -z "$pid" ]; then
    echo "[$APP_NAME] 未运行"
    rm -f "$PID_FILE"
    return 0
  fi

  echo "[$APP_NAME] 停止中 (PID $pid)..."
  kill -TERM -- "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null

  for ((i = 1; i <= STOP_TIMEOUT; i++)); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 1
  done

  if kill -0 "$pid" 2>/dev/null; then
    echo "[$APP_NAME] ${STOP_TIMEOUT}s 内未退出，强制 kill -9"
    kill -KILL -- "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null
    sleep 1
  fi

  left="$(pgrep -f "$MATCH")"
  if [ -n "$left" ]; then
    echo "[$APP_NAME] 清理残留进程: $left"
    kill -KILL $left 2>/dev/null
  fi

  rm -f "$PID_FILE"
  echo "[$APP_NAME] 已停止"
}

status() {
  local pid
  pid="$(get_pid)"
  if [ -n "$pid" ]; then
    echo "[$APP_NAME] 运行中 (PID $pid)"
    ps -o pid,ppid,user,etime,%cpu,%mem,rss,args --sid "$pid"
    return 0
  fi
  echo "[$APP_NAME] 未运行"
  return 3
}

case "${1:-}" in
  start)   check_user; start ;;
  stop)    check_user; stop ;;
  restart) check_user; stop; start ;;
  status)  status ;;
  log)     tail -n 200 -f "$LOG_FILE" ;;
  *)       echo "用法: $0 {start|stop|restart|status|log}"; exit 1 ;;
esac