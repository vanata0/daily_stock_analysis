#!/bin/bash
# daily_stock_analysis 重启脚本
# 用法: ./restart.sh [serve|schedule|run]
set -e

PROJ_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJ_DIR"

TARGET="${1:-serve}"
PID_FILE="$PROJ_DIR/.server.pid"
LOG_FILE="$PROJ_DIR/logs/server.log"

RED='\033[0;31m'; GREEN='\033[0;32m'; CYAN='\033[0;36m'; NC='\033[0m'
info() { echo -e "${CYAN}ℹ ${NC}$*"; }
ok()   { echo -e "${GREEN}✔ ${NC}$*"; }
die()  { echo -e "${RED}✘ ${NC}$*"; exit 1; }

# ── 停止旧进程（仅本项目）────────────────────
if [ -f "$PID_FILE" ]; then
  OLD_PID=$(cat "$PID_FILE")
  if kill -0 "$OLD_PID" 2>/dev/null; then
    info "停止旧进程 (PID $OLD_PID)..."
    kill -TERM "$OLD_PID" 2>/dev/null || true
    sleep 2
    kill -0 "$OLD_PID" 2>/dev/null && kill -9 "$OLD_PID" 2>/dev/null || true
    ok "旧进程已终止"
  fi
  rm -f "$PID_FILE"
fi
fuser -k 8000/tcp 2>/dev/null || true

# ── 激活虚拟环境 ─────────────────────────────
[ -f venv/bin/activate ] || die "未找到 venv/，请先运行: python -m venv venv && pip install -r requirements.txt"
source venv/bin/activate

# ── 启动 ─────────────────────────────────────
mkdir -p logs
case "$TARGET" in
  serve|web|api)
    info "启动 API 服务（后台）..."
    nohup python3 main.py --serve-only >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    ok "server 已启动 (PID $(cat $PID_FILE))，日志: logs/server.log"
    ;;
  schedule)
    info "启动定时任务（后台）..."
    nohup python3 main.py --schedule >> "$LOG_FILE" 2>&1 &
    echo $! > "$PID_FILE"
    ok "schedule 已启动 (PID $(cat $PID_FILE))，日志: logs/server.log"
    ;;
  run)
    info "执行单次分析..."
    python3 main.py
    ;;
  stop)
    ok "服务已停止（无需重启）"
    ;;
  *)
    echo "用法: ./restart.sh [serve|schedule|run|stop]"
    exit 1
    ;;
esac
