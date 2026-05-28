#!/usr/bin/env bash
# backup_db.sh — 备份 tracking.db 到 backups/ 目录
# 用法: ./scripts/backup_db.sh [output_dir]
#       默认备份到 /opt/zoom-monitor/backups/

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
OUTPUT_DIR="${1:-$BASE_DIR/backups}"
DB_PATH="${ZOOM_DB_PATH:-$BASE_DIR/data/tracking.db}"
TIMESTAMP=$(date '+%Y%m%d_%H%M%S')
BACKUP_FILE="$OUTPUT_DIR/zoom-monitor-$TIMESTAMP.sqlite"

mkdir -p "$OUTPUT_DIR"

if [ ! -f "$DB_PATH" ]; then
    echo "ERROR: Database not found: $DB_PATH"
    exit 1
fi

if ! command -v sqlite3 &>/dev/null; then
    echo "ERROR: sqlite3 not found. Install with: apt install sqlite3"
    exit 1
fi

# 使用 sqlite3 .backup 命令（在线安全，不锁库）
sqlite3 "$DB_PATH" ".backup '$BACKUP_FILE'"

# 验证
if [ -f "$BACKUP_FILE" ]; then
    SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
    RECORDS=$(sqlite3 "$BACKUP_FILE" "SELECT COUNT(*) FROM zoom_participants;" 2>/dev/null || echo "?")
    echo "Backup complete: $BACKUP_FILE"
    echo "  Size: $SIZE"
    echo "  Participants: $RECORDS"
else
    echo "ERROR: Backup failed — output file not created"
    exit 1
fi

# 清理旧备份（保留最近 30 天）
find "$OUTPUT_DIR" -name "zoom-monitor-*.sqlite" -mtime +30 -delete 2>/dev/null || true
echo "Cleaned backups older than 30 days"
