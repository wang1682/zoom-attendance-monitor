# Zoom Attendance Monitor — v1.0.1-lite

> 让参会管理像喝水一样简单 · 自动记录参会 · Telegram 实时预警 · AI 出勤分析

## 更新内容 (v1.0.0-lite → v1.0.1-lite)

### 🔧 生产环境加固

- **API_HOST 修复** — 默认改为 `0.0.0.0`，解决 Docker 容器外无法访问的问题
- **Webhook 解析修复** — 正确处理 Zoom v2 事件格式嵌套 `payload.object`，参会数据不再丢失
- **健康检查修复** — command/monitor 容器从 `pgrep` 改为 `kill -0 1`，适配 slim 镜像
- **`docker-compose.yml` 清理** — 移除废弃的 `version: "3.9"` 字段，消除警告

### 📦 Docker 优先部署

- **README 重构** — Docker Compose 作为默认部署方式，systemd 降级为 Legacy/手动模式
- **install.sh 更新** — 默认选项改为 Docker，端口/路径全部同步 Docker
- **`.env.example` 更新** — 默认路径指向 Docker volume，附带 systemd 备选注释

### 🔒 安全审计

| 审计项 | 结果 |
|--------|------|
| 源码硬编码密钥扫描 | ✅ PASS |
| 发布包密钥检查 | ✅ PASS |
| .env.example 安全 | ✅ PASS |
| 演示数据安全 | ✅ PASS |
| install.sh 安全性 | ✅ PASS |
| 审计文档完整性 | ✅ PASS |

**Build hash:** `15cf32e4`

---

## 快速开始 (Docker 推荐)

```bash
# 环境准备
sudo mkdir -p /opt/zoom-monitor
cd /opt/zoom-monitor

# 下载 tarball 或 git clone 后：
cp .env.example .env && chmod 600 .env
# 编辑 .env 填入凭据

# 一行启动
docker compose up -d

# 验证
scripts/check_health.sh
```

详细文档见 [README.md](README.md)

---

## 技术栈

- **Runtime:** Python 3.12 / FastAPI / uvicorn
- **Database:** SQLite 3.46+ (WAL mode)
- **Bot:** python-telegram-bot v21.x
- **Zoom:** REST API v2 (Server-to-Server OAuth)
- **AI Reports:** DeepSeek / OpenAI API (optional)
- **Deploy:** Docker Compose (primary) / systemd (legacy)
- **Tunnel:** Cloudflare Tunnel (recommended)
- **Base Image:** python:3.12-slim

---
