#!/usr/bin/env bash
# check_health.sh — 系统健康检查脚本
# 检查：API / Webhook / Monitor / Command 四个服务 + 数据库

set -euo pipefail

BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"
PASS=0
FAIL=0

green() { printf "\033[32m%s\033[0m\n" "$1"; }
red()   { printf "\033[31m%s\033[0m\n" "$1"; }

check() {
    local name="$1" result="$2"
    if [ "$result" = "ok" ]; then
        green "  ✓ $name"
        PASS=$((PASS + 1))
    else
        red "  ✗ $name"
        FAIL=$((FAIL + 1))
    fi
}

echo "=== Zoom Monitor Health Check ==="
echo "Date: $(date '+%Y-%m-%d %H:%M:%S %Z')"
echo ""

# 1. systemd 服务状态
echo "[Services]"
for svc in zoom-api zoom-webhook zoom-monitor zoom-command; do
    st=$(systemctl is-active "$svc" 2>/dev/null || echo "not found")
    check "$svc" "$st"
done
echo ""

# 2. HTTP 端点
echo "[HTTP Endpoints]"
API_URL="${ZOOM_API_URL:-http://127.0.0.1:8000}"
WH_URL="${ZOOM_WEBHOOK_URL:-http://127.0.0.1:9000}"

if curl -sf "$API_URL/health" >/dev/null 2>&1; then
    check "API ($API_URL/health)" "ok"
else
    check "API ($API_URL/health)" "fail"
fi

if curl -sf "$WH_URL/health" >/dev/null 2>&1; then
    check "Webhook ($WH_URL/health)" "ok"
else
    check "Webhook ($WH_URL/health)" "fail"
fi
echo ""

# 3. 数据库
echo "[Database]"
DB_PATH="${ZOOM_DB_PATH:-$BASE_DIR/data/tracking.db}"
if [ -f "$DB_PATH" ]; then
    # 检查关键表
    TABLE_COUNT=$(sqlite3 "$DB_PATH" "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%';" 2>/dev/null || echo "0")
    if [ "$TABLE_COUNT" -ge 5 ]; then
        check "DB $DB_PATH ($TABLE_COUNT tables)" "ok"
    else
        check "DB $DB_PATH ($TABLE_COUNT tables, expected 5+)" "warn"
    fi
else
    check "DB $DB_PATH (not found)" "fail"
fi
echo ""

# 4. .env 存在性和权限
echo "[Config]"
ENV_PATH="$BASE_DIR/.env"
if [ -f "$ENV_PATH" ]; then
    PERMS=$(stat -c "%a %U" "$ENV_PATH" 2>/dev/null || stat -f "%Lp %Su" "$ENV_PATH" 2>/dev/null)
    check ".env ($PERMS)" "ok"
else
    check ".env (not found)" "fail"
fi
echo ""

# 5. Python 编译检查
echo "[Code Integrity]"
PY_FAIL=0
for f in "$BASE_DIR"/*.py; do
    if ! python3 -m py_compile "$f" 2>/dev/null; then
        red "  ✗ $(basename "$f") (compile error)"
        PY_FAIL=$((PY_FAIL + 1))
    fi
done
if [ "$PY_FAIL" -eq 0 ]; then
    check "All Python files" "ok"
else
    check "$PY_FAIL Python file(s) have errors" "fail"
fi
echo ""

# 结果
echo "================================"
total=$((PASS + FAIL))
if [ "$FAIL" -eq 0 ]; then
    green "Result: $PASS/$PASS passed ✓"
    exit 0
else
    red "Result: $PASS/$total passed, $FAIL failed"
    exit 1
fi
