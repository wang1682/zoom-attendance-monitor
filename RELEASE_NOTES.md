# Zoom Attendance Monitor — v1.0.0-lite

> 让参会管理像喝水一样简单 · 自动记录参会 · Telegram 实时预警 · AI 出勤分析

## 核心特性

### 📹 双源参会监控
- **Zoom API v2 Polling** — 每 30 秒拉取参会列表，零漏录
- **Webhook 事件回调** — 实时接收 `Meeting Participant Joined / Left` 事件
- **双通道冗余** — Webhook 主通道 + Polling 回填，网络抖动不掉数据

### 🤖 Telegram 智能推送
- **进出实时推送** — 谁进来了、谁走了，即时推送到你手机
- **陌生人检测** — 首次出现的邮箱自动标记 + 首现告警
- **签到提醒** — `SIGNIN_DEADLINE_HOUR` 自动标记迟到
- **推送时段控制** — `PUSH_START_HOUR` / `PUSH_END_HOUR` 避免非工作时间骚扰
- **静默模式** — 临时关推送不关记录
- **品牌化推送模板** — 全部文案通过 brand.json 自定义，无需改代码

### 📊 品牌化 Web 看板
- **FastAPI 驱动的仪表盘** — 实时数据看板 + 历史记录查询
- **暗色主题 + 品牌配色** — 通过 brand.json 自定义颜色、名称、Logo
- **RBAC 角色管理** — admin / viewer / operator
- **多租户支持** — 一套部署服务 N 个会议室/组织

### 🧠 AI 出勤分析（可选）
- **每日/每周自动化报告** — DeepSeek / OpenAI API 驱动
- **出勤风险分析** — 迟到率、旷工趋势、异常识别
- **报告定时推送到 Telegram**

### 🔒 安全架构
- **密钥外置** — 全部密钥集中在 `.env`，chmod 600 保护
- **systemd EnvironmentFile** — 不暴露在进程列表
- **Webhook 签名验证** — Zoom Webhook Secret HMAC 校验
- **安全审计 6/6 PASS** — 源码 / 发布包 / env.example / 演示数据 / 安装脚本全清
- **Docker 非 root 用户运行** — 最小权限原则

### ⚡ 部署灵活
- **systemd 四服务架构** — api / webhook / monitor / command
- **Docker Compose 四容器** — 环境隔离
- **Cloudflare Tunnel 兼容** — 无需公网 IP
- **SQLite 持久化** — 零外部依赖
- **三分钟部署** — cp .env → pip install → systemctl start

---

## 安全审计结果

| 审计项 | 结果 |
|--------|------|
| 源码硬编码密钥扫描 | ✅ PASS |
| 发布包密钥检查 | ✅ PASS |
| .env.example 安全 | ✅ PASS |
| 演示数据安全 | ✅ PASS |
| install.sh 安全性 | ✅ PASS |
| 审计文档完整性 | ✅ PASS |

**Build hash:** `8c66c8371d6c491ac6cf7499781239154a5aeffa866d7d6739df76a4540ecf1a`

---

## 快速开始

```bash
# 下载发布包
curl -LO https://github.com/your-org/zoom-attendance-monitor/releases/download/v1.0.0-lite/zoom-monitor-v1.0.0-lite.tar.gz
tar -xzf zoom-monitor-v1.0.0-lite.tar.gz
cd zoom-monitor-v1.0.0-lite

# 配置 → 部署 → 检查
cp .env.example .env && chmod 600 .env
pip install -r requirements.txt
sudo systemctl enable --now zoom-{api,webhook,monitor,command}
scripts/check_health.sh
```

详细安装文档：[docs/INSTALL.md](docs/INSTALL.md)

---

## 技术栈

- **Runtime:** Python 3.10+ / FastAPI / uvicorn
- **Database:** SQLite 3.46+ (WAL mode)
- **Bot:** python-telegram-bot v21.x
- **Zoom:** zoom-python-sdk / REST API v2
- **AI Reports:** DeepSeek / OpenAI API (optional)
- **Deploy:** systemd / Docker + Compose
- **Tunnel:** Cloudflare Tunnel (recommended)

---

**给 Star ⭐** 如果这个项目对你有帮助！
**提 Issue 🐛** 如果遇到问题或有建议！
