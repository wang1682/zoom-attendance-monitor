#!/usr/bin/env bash
# Zoom Attendance Monitor — 一键安装脚本
# Usage: curl -fsSL https://raw.githubusercontent.com/your-org/zoom-attendance-monitor/v1.0.0-lite/install.sh | bash
#
# 工作方式：
# 1. 检测系统依赖（Docker 或 Python）
# 2. 提示安装方式（Docker / systemd / 裸跑）
# 3. 下载 Release 包
# 4. 配置 .env
# 5. 启动服务

set -euo pipefail

RELEASE_VERSION="v1.0.0-lite"
REPO_URL="https://github.com/your-org/zoom-attendance-monitor"
INSTALL_DIR="/opt/zoom-monitor"
RELEASE_TAR="${RELEASE_VERSION}.tar.gz"
RELEASE_URL="${REPO_URL}/releases/download/${RELEASE_VERSION}/${RELEASE_TAR}"

CYAN='\033[36m'
GREEN='\033[32m'
YELLOW='\033[33m'
RED='\033[31m'
NC='\033[0m'

log()  { echo -e "${CYAN}[ZOOM]${NC} $1"; }
ok()   { echo -e "${GREEN}  ✓${NC} $1"; }
warn() { echo -e "${YELLOW}  ⚠${NC} $1"; }
fail() { echo -e "${RED}  ✗${NC} $1"; exit 1; }

# ─── Banner ─────────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}"
echo "  ╔═══════════════════════════════════════╗"
echo "  ║   Zoom Attendance Monitor             ║"
echo "  ║   ${RELEASE_VERSION}                        ║"
echo "  ║   自动参会记录 · Telegram 实时预警     ║"
echo "  ╚═══════════════════════════════════════╝"
echo -e "${NC}"
echo ""

# ─── 系统检测 ───────────────────────────────────────────────────────────
log "检测系统环境..."

OS="unknown"
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
fi
ok "OS: $OS ($(uname -m))"

HAS_DOCKER=false
HAS_PYTHON=false
HAS_SYSTEMD=false

if command -v docker &>/dev/null; then
    HAS_DOCKER=true
    ok "Docker: $(docker --version 2>/dev/null | head -1)"
fi

if command -v python3 &>/dev/null; then
    PY_VER=$(python3 --version 2>/dev/null | head -1)
    HAS_PYTHON=true
    ok "Python: $PY_VER"
fi

if command -v systemctl &>/dev/null; then
    HAS_SYSTEMD=true
    ok "systemd: 可用"
fi

# ─── 安装方式选择 ────────────────────────────────────────────────────────
log ""
log "选择部署方式:"
echo "  1) Docker Compose（推荐——隔离环境）"
echo "  2) systemd + Python 虚拟环境（生产性能最佳）"
echo "  3) 裸跑 Python（调试/开发）"
echo ""
read -r -p "  请输入 [1-3] (默认 1): " DEPLOY_MODE </dev/tty
DEPLOY_MODE="${DEPLOY_MODE:-1}"

case "$DEPLOY_MODE" in
    1)
        if ! $HAS_DOCKER; then
            warn "Docker 未安装，尝试自动安装..."
            curl -fsSL https://get.docker.com | bash
            $HAS_DOCKER=true
        fi
        INSTALL_MODE="docker"
        ;;
    2)
        if ! $HAS_SYSTEMD; then
            fail "systemd 不可用，无法选择此方式"
        fi
        if ! $HAS_PYTHON; then
            fail "Python3 未安装，无法选择此方式"
        fi
        INSTALL_MODE="systemd"
        ;;
    3)
        if ! $HAS_PYTHON; then
            fail "Python3 未安装"
        fi
        INSTALL_MODE="bare"
        warn "裸跑模式：不会自动重启，建议 screen/tmux"
        ;;
    *)
        fail "无效选项: $DEPLOY_MODE"
        ;;
esac

# ─── 下载 Release ────────────────────────────────────────────────────────
log ""
log "下载 Release 包..."

sudo mkdir -p "$INSTALL_DIR"
cd "$INSTALL_DIR"

# 尝试下载 Release tarball
if curl -fsSL -o "${RELEASE_TAR}" "${RELEASE_URL}" 2>/dev/null; then
    tar -xzf "${RELEASE_TAR}" -C "$INSTALL_DIR" --strip-components=1
    rm -f "${RELEASE_TAR}"
    ok "Release 包已下载并解压"
else
    warn "Release 包下载失败，尝试从 GitHub 直接克隆..."
    if command -v git &>/dev/null; then
        git clone --depth 1 --branch "$RELEASE_VERSION" "$REPO_URL" /tmp/zoom-monitor-tmp
        cp -r /tmp/zoom-monitor-tmp/* "$INSTALL_DIR"
        rm -rf /tmp/zoom-monitor-tmp
        ok "Git 克隆完成"
    else
        warn "Git 不可用。从当前目录复制文件..."
        # 如果脚本是伴随源码分发的，直接复制
        SCRIPT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
        if [ "$SCRIPT_DIR" != "$INSTALL_DIR" ]; then
            cp -r "$SCRIPT_DIR"/* "$INSTALL_DIR"
        fi
        ok "本地文件已复制"
    fi
fi

# ─── 配置 .env ───────────────────────────────────────────────────────────
log ""
log "配置 .env..."

if [ -f "$INSTALL_DIR/.env" ]; then
    warn ".env 已存在，跳过"
else
    cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
    chmod 600 "$INSTALL_DIR/.env"
    ok ".env 已创建 (${INSTALL_DIR}/.env)"

    log ""
    log "请编辑 .env 填入 Zoom 凭据和 Telegram Token:"
    log "  vim ${INSTALL_DIR}/.env"
    log ""
    log "需要准备:"
    log "  · Zoom Account ID（Server-to-Server OAuth）"
    log "  · Zoom Client ID"
    log "  · Zoom Client Secret"
    log "  · Telegram Bot Token（@BotFather）"
    log "  · Telegram Chat ID（@userinfobot）"
    log ""
    read -r -p "  准备好后按 Enter 继续..." </dev/tty
fi

# ─── 安装依赖 ────────────────────────────────────────────────────────────
log ""
log "安装依赖..."

case "$INSTALL_MODE" in
    docker)
        # Docker：只需下载镜像
        docker compose pull 2>/dev/null || docker compose build
        ok "Docker 镜像准备完成"
        ;;
    systemd|bare)
        # Python 虚拟环境
        if [ ! -d "venv" ]; then
            python3 -m venv venv
            ok "虚拟环境已创建"
        fi
        source venv/bin/activate
        pip install --quiet --upgrade pip
        pip install --quiet -r requirements.txt
        ok "Python 依赖已安装"

        if [ "$INSTALL_MODE" = "systemd" ]; then
            # 安装 systemd 服务
            for svc in zoom-api zoom-webhook zoom-monitor zoom-command; do
                sudo cp "$INSTALL_DIR/systemd/${svc}.service" /etc/systemd/system/
                sudo sed -i "s|ExecStart=.*|ExecStart=$(pwd)/venv/bin/python3 $(pwd)/app.py ${svc#zoom-}|" "/etc/systemd/system/${svc}.service"
                sudo sed -i "s|WorkingDirectory=.*|WorkingDirectory=$(pwd)|" "/etc/systemd/system/${svc}.service"
            done
            sudo systemctl daemon-reload
            ok "systemd 服务单元已安装"
        fi
        ;;
esac

# ─── 启动 ────────────────────────────────────────────────────────────────
log ""
log "启动服务..."

case "$INSTALL_MODE" in
    docker)
        docker compose up -d
        docker compose ps
        ;;
    systemd)
        sudo systemctl enable --now zoom-{api,webhook,monitor,command}
        sudo systemctl status zoom-{api,webhook,monitor,command}
        ;;
    bare)
        warn "启动 4 个终端:"
        echo "  tmux new -s zoom-api     -d 'python3 app.py api'"
        echo "  tmux new -s zoom-webhook  -d 'python3 app.py webhook'"
        echo "  tmux new -s zoom-monitor  -d 'python3 app.py monitor'"
        echo "  tmux new -s zoom-command  -d 'python3 app.py command'"
        ;;
esac

# ─── 验证 ────────────────────────────────────────────────────────────────
log ""
log "验证..."

sleep 3
if curl -sf http://127.0.0.1:8000/health >/dev/null 2>&1; then
    ok "API 服务运行中 (http://127.0.0.1:8000)"
else
    warn "API 暂不可达（可能端口不同）"
fi

# ─── 完成 ────────────────────────────────────────────────────────────────
log ""
echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  Zoom Attendance Monitor 安装完成!${NC}"
echo -e "${GREEN}========================================${NC}"
echo ""
echo "  Dashboard: http://localhost:8000"
echo "  文档:      ${INSTALL_DIR}/docs/"
echo "  .env:      ${INSTALL_DIR}/.env"
echo ""
echo "  Telegram Bot 指令:"
echo "    /start   — 初始化"
echo "    /status  — 系统状态"
echo "    /enable  — 开启推送"
echo "    /disable — 关闭推送"
echo ""
echo "  健康检查:"
echo "    bash ${INSTALL_DIR}/scripts/check_health.sh"
echo ""
echo -e "${YELLOW}  别忘了把 Bot 加入群聊并配置 TELEGRAM_GROUP_CHAT_ID${NC}"
echo ""
