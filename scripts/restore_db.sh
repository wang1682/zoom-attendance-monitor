#!/usr/bin/env bash
# restore_db.sh — 从备份文件恢复 tracking.db
# 用法: ./scripts/restore_db.sh <backup_file>
#       恢复前自动备份当前库

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
DB_PATH="${ZOOM_DB_PATH:-$BASE_DIR/data/tracking.db}"
BACKUP_FILE="${1:-}"

if [ -z "$BACKUP_FILE" ]; then
    echo "Usage: $0 <backup_file>"
    echo "Example: $0 $BASE_DIR/backups/zoom-monitor-20250528.sqlite"
    exit 1
fi

if [ ! -f "$BACKUP_FILE" ]; then
    echo "ERROR: Backup file not found: $BACKUP_FILE"
    exit 1
fi

if ! command -v sqlite3 &>/dev/null; then
    echo "ERROR: sqlite3 not found. Install with: apt install sqlite3"
    exit 1
fi

# 验证备份文件完整性
echo "Verifying backup integrity..."
if ! sqlite3 "$BACKUP_FILE" "SELECT COUNT(*) FROM zoom_participants;" >/dev/null 2>&1; then
    echo "ERROR: Backup file is not a valid SQLite database or corrupted"
    exit 1
fi

RECORDS=$(sqlite3 "$BACKUP_FILE" "SELECT COUNT(*) FROM zoom_participants;")
echo "Backup contains $RECORDS participant records"

# 自动备份当前库
if [ -f "$DB_PATH" ]; then
    AUTO_BACKUP="$BASE_DIR/backups/pre-restore-$(date '+%Y%m%d_%H%M%S').sqlite"
    mkdir -p "$BASE_DIR/backups"
    sqlite3 "$DB_PATH" ".backup '$AUTO_BACKUP'"
    echo "Current database backed up to: $AUTO_BACKUP"
fi

# 恢复
echo "Restoring from: $BACKUP_FILE"
sqlite3 "$DB_PATH" ".restore '$BACKUP_FILE'"

echo "Restore complete!"
echo "  Target: $DB_PATH"
echo "  Source: $BACKUP_FILE"
echo "  Records: $RECORDS participants"

# 提示重启
echo ""
echo "NOTE: Restart services to pick up restored database:"
echo "  sudo systemctl restart zoom-{api,webhook,monitor,command}"
echo "  or: docker compose restart"
