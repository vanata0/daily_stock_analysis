#!/bin/bash
# daily_stock_analysis 快速启动脚本
cd "$(dirname "$0")"
source venv/bin/activate

case "$1" in
  web)
    echo "🚀 启动 Web 管理界面..."
    python main.py --webui
    ;;
  run)
    echo "📊 执行单次分析..."
    python main.py
    ;;
  serve)
    echo "🖥️ 启动 API 服务 (端口 8000)..."
    python main.py --serve-only
    ;;
  dry)
    echo "🔍 试运行（只获取数据，不分析）..."
    python main.py --dry-run
    ;;
  debug)
    echo "🐛 调试模式..."
    python main.py --debug
    ;;
  schedule)
    echo "⏰ 定时任务模式..."
    python main.py --schedule
    ;;
  *)
    echo "用法: ./start.sh [web|run|serve|dry|debug|schedule]"
    echo ""
    echo "  web      - Web 管理界面 (默认)"
    echo "  run      - 单次分析"
    echo "  serve    - API 服务"
    echo "  dry      - 试运行"
    echo "  debug    - 调试模式"
    echo "  schedule - 定时任务"
    ;;
esac
