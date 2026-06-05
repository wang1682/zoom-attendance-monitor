#!/usr/bin/env bash
set -euo pipefail

APP_DIR="/opt/zoom-attendance-monitor"
BACKUP_DIR="$APP_DIR/backups"
TS="$(date +%F_%H%M%S)"

mkdir -p "$BACKUP_DIR"

docker run --rm \
  -v zoom-attendance-monitor_zoom_data:/data:ro \
  -v "$BACKUP_DIR":/backup \
  alpine sh -c "apk add --no-cache sqlite >/dev/null 2>&1; sqlite3 /data/tracking.db '.backup /backup/tracking_${TS}.db' && gzip /backup/tracking_${TS}.db"

find "$BACKUP_DIR" -name "tracking_*.db.gz" -mtime +14 -delete
echo "backup ok: $BACKUP_DIR/tracking_${TS}.db.gz"
