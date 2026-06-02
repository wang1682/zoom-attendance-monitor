#!/bin/sh
# entrypoint.sh — 根据 APP_MODE 启动对应服务
set -e

MODE="${APP_MODE:-api}"

# 如果 .env 存在则使用，否则全靠环境变量
[ -f .env ] && . ./.env

# Docker 环境覆盖 DATABASE_URL 为容器内路径（volume mount 到 /app/data）
export DATABASE_URL="sqlite+aiosqlite:////app/data/tracking.db"

# 确保 data 目录存在
mkdir -p /app/data /app/logs /app/backup

case "$MODE" in
  api)
    exec python app.py api
    ;;
  webhook)
    exec python app.py webhook
    ;;
  monitor)
    exec python app.py monitor
    ;;
  command)
    exec python app.py command
    ;;
  *)
    echo "Unknown APP_MODE: $MODE (valid: api, webhook, monitor, command)"
    exit 1
    ;;
esac
