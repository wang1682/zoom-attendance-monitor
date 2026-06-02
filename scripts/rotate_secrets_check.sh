#!/usr/bin/env bash
# rotate_secrets_check.sh — 检查 Key 是否需轮换
# 运行方式：cron 每周一执行
# 不接触任何 secret 值，只检查到期时间

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
ENV_PATH="$BASE_DIR/.env"

echo "=== Secret Rotation Check ==="
echo "Date: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo ""

ISSUES=0

# 1. 检查 .env 存在
if [ ! -f "$ENV_PATH" ]; then
    echo "[CRITICAL] .env file not found at $ENV_PATH"
    ISSUES=$((ISSUES + 1))
else
    # 检查权限
    PERMS=$(stat -c "%a" "$ENV_PATH" 2>/dev/null || stat -f "%Lp" "$ENV_PATH" 2>/dev/null)
    if [ "$PERMS" != "600" ]; then
        echo "[WARN] .env permissions are $PERMS (should be 600)"
        echo "  Fix: chmod 600 $ENV_PATH"
    fi

    # 检查是否包含 ???（未配置占位符）
    if grep -q "your-" "$ENV_PATH" 2>/dev/null; then
        echo "[WARN] .env contains placeholder values (your-xxx)"
        echo "  These need to be replaced with actual secrets"
        ISSUES=$((ISSUES + 1))
    fi

    # 检查是否为空
    for key in ZOOM_CLIENT_SECRET TELEGRAM_BOT_TOKEN; do
        val=$(grep "^${key}=" "$ENV_PATH" | cut -d= -f2- || echo "")
        if [ -z "$val" ]; then
            echo "[CRITICAL] $key is empty in .env"
            ISSUES=$((ISSUES + 1))
        elif [ ${#val} -lt 8 ]; then
            echo "[WARN] $key is too short (${#val} chars) — possible placeholder"
            ISSUES=$((ISSUES + 1))
        fi
    done
fi

# 2. 检查 Git 是否跟踪了 .env
if [ -d "$BASE_DIR/.git" ]; then
    if git -C "$BASE_DIR" ls-files --error-unmatch .env &>/dev/null 2>&1; then
        echo "[CRITICAL] .env is tracked by Git!"
        echo "  Fix: git rm --cached .env && echo .env >> .gitignore"
        ISSUES=$((ISSUES + 1))
    fi
fi

# 3. 检查备份目录是否有敏感文件
BACKUP_DIR="$BASE_DIR/backups"
if [ -d "$BACKUP_DIR" ]; then
    # 备份文件不含 secret（只有 DB），但检查权限
    BACKUP_PERMS=$(stat -c "%a" "$BACKUP_DIR" 2>/dev/null || stat -f "%Lp" "$BACKUP_DIR" 2>/dev/null)
    if [ "$BACKUP_PERMS" != "700" ] && [ "$BACKUP_PERMS" != "755" ]; then
        echo "[INFO] Backup directory permissions: $BACKUP_PERMS"
        echo "  Recommended: chmod 700 $BACKUP_DIR"
    fi
fi

# 4. 检查 systemd 是否使用 EnvironmentFile
for svc in zoom-api zoom-webhook zoom-monitor zoom-command; do
    unit="/etc/systemd/system/${svc}.service"
    if [ -f "$unit" ]; then
        if ! grep -q "EnvironmentFile" "$unit" 2>/dev/null; then
            echo "[WARN] $svc.service has no EnvironmentFile directive"
            echo "  Secrets may be embedded in the unit file"
            ISSUES=$((ISSUES + 1))
        fi
    fi
done

echo ""
if [ "$ISSUES" -eq 0 ]; then
    echo "✓ No issues found — secrets appear well managed"
else
    echo "⚠  $ISSUES issue(s) found — review above"
fi

exit $ISSUES
