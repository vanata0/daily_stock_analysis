#!/bin/bash
# =============================================
# daily_stock_analysis 重启脚本
# 用法: ./reset.sh [docker|local] [serve|schedule|web]
# =============================================
set -e

cd "$(dirname "$0")"

MODE="${1:-auto}"      # docker | local | auto
TARGET="${2:-serve}"   # serve | schedule | web | all

# ── 颜色输出 ──────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()    { echo -e "${CYAN}ℹ ${NC}$*"; }
ok()      { echo -e "${GREEN}✔ ${NC}$*"; }
warn()    { echo -e "${YELLOW}⚠ ${NC}$*"; }
die()     { echo -e "${RED}✘ ${NC}$*"; exit 1; }

# ── 自动检测模式 ──────────────────────────────
if [ "$MODE" = "auto" ]; then
  if docker compose -f docker/docker-compose.yml ps --quiet 2>/dev/null | grep -q .; then
    MODE="docker"
  elif [ -f venv/bin/activate ]; then
    MODE="local"
  else
    MODE="docker"   # 默认尝试 docker
  fi
  info "自动检测模式: $MODE"
fi

# ══════════════════════════════════════════════
# Docker 模式
# ══════════════════════════════════════════════
restart_docker() {
  info "停止容器..."
  docker compose -f docker/docker-compose.yml down || true

  info "重新构建镜像（如有代码变更）..."
  docker compose -f docker/docker-compose.yml build --quiet

  case "$TARGET" in
    serve|web|api)
      info "启动 server 容器（FastAPI 模式）..."
      docker compose -f docker/docker-compose.yml up -d server
      ok "server 已启动，API 端口: ${API_PORT:-8000}"
      ;;
    schedule|analyzer)
      info "启动 analyzer 容器（定时任务模式）..."
      docker compose -f docker/docker-compose.yml up -d analyzer
      ok "analyzer 已启动"
      ;;
    all)
      info "启动所有容器..."
      docker compose -f docker/docker-compose.yml up -d
      ok "所有容器已启动"
      ;;
    *)
      warn "未知 target '$TARGET'，启动 server..."
      docker compose -f docker/docker-compose.yml up -d server
      ;;
  esac

  echo ""
  info "容器状态:"
  docker compose -f docker/docker-compose.yml ps
}

# ══════════════════════════════════════════════
# 本地 venv 模式
# ══════════════════════════════════════════════
restart_local() {
  # 找到正在运行的 main.py 进程并停止
  OLD_PIDS=$(pgrep -f "python.*main\.py" 2>/dev/null || true)
  if [ -n "$OLD_PIDS" ]; then
    info "终止旧进程: $OLD_PIDS"
    echo "$OLD_PIDS" | xargs kill -TERM 2>/dev/null || true
    sleep 2
    # 若仍存活则强杀
    STILL=$(pgrep -f "python.*main\.py" 2>/dev/null || true)
    [ -n "$STILL" ] && echo "$STILL" | xargs kill -9 2>/dev/null || true
    ok "旧进程已终止"
  else
    info "没有正在运行的 main.py 进程"
  fi

  # 激活虚拟环境
  [ -f venv/bin/activate ] || die "未找到 venv/bin/activate，请先运行: python -m venv venv && pip install -r requirements.txt"
  source venv/bin/activate

  case "$TARGET" in
    serve|web|api)
      info "启动 API 服务（后台）..."
      nohup python main.py --serve-only > logs/server.log 2>&1 &
      ok "server 已启动 (PID $!)，日志: logs/server.log"
      ;;
    schedule)
      info "启动定时任务模式（后台）..."
      nohup python main.py --schedule > logs/schedule.log 2>&1 &
      ok "schedule 已启动 (PID $!)，日志: logs/schedule.log"
      ;;
    run)
      info "执行单次分析..."
      python main.py
      ;;
    *)
      info "启动 API 服务（后台）..."
      nohup python main.py --serve-only > logs/server.log 2>&1 &
      ok "server 已启动 (PID $!)，日志: logs/server.log"
      ;;
  esac
}

# ── 入口 ──────────────────────────────────────
case "$MODE" in
  docker) restart_docker ;;
  local)  restart_local  ;;
  *)      die "未知模式 '$MODE'。用法: ./reset.sh [docker|local] [serve|schedule|web|all]" ;;
esac
