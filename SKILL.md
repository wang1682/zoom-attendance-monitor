---
name: zoom-attend-platform
description: 部署 Zoom Attendance Intelligence Platform — FastAPI + SQLAlchemy + 多租户 SaaS 架构的会议行为监控平台。
---

# Zoom Attendance Intelligence Platform

会议行为监控 SaaS 平台。支持多租户、Webhook 事件落库、Telegram 实时预警、轮询监控、行为分析、看板 API。

## 架构

```
/opt/zoom-monitor/
├── .env                          # 全部 secrets 外置
├── zoom_monitor.py               # 兼容入口 → app/
├── start_monitor.py              # 轮询监控启动入口
├── start_webhook.py              # Webhook 接收服务 (port 9000)
├── start_api.py                  # REST API (port 8000)
├── pyproject.toml                # 依赖管理
├── alembic.ini                   # Alembic 迁移配置
├── app/
│   ├── main.py                   # FastAPI 应用
│   ├── settings.py               # pydantic-settings 配置中心
│   ├── database.py               # Session 工厂 + 初始化
│   ├── models/
│   │   └── __init__.py           # ORM 模型（含 tenant_id）
│   ├── services/
│   │   ├── event_service.py      # 事件写入 + 陌生邮箱检测
│   │   ├── monitor_service.py    # Zoom 轮询监控主逻辑
│   │   └── __init__.py
│   ├── integrations/
│   │   ├── zoom/
│   │   │   └── api.py            # Zoom Server-to-Server OAuth 客户端
│   │   └── telegram/
│   │       └── __init__.py       # Telegram 推送
│   ├── api/
│   │   └── v1/
│   │       ├── __init__.py
│   │       ├── endpoints.py      # REST API 端点（看板/排名/预警）
│   │       └── webhook.py        # Zoom Webhook 接收端点
│   └── alerts/                   # 告警引擎（预留）
│   └── analytics/                # AI 行为分析（预留）
│   └── workers/                  # Celery Worker（预留）
├── data/
│   └── tracking.db               # SQLite 数据库
├── migrations/                   # Alembic 迁移文件
├── scripts/
│   └── migrate_db.py             # 旧 tracking.db → 新 ORM 迁移
└── tests/
```

## 三服务 systemd

| Unit | 用途 | 端口 |
|------|------|------|
| zoom-monitor.service | 轮询监控（异步主循环） | — |
| zoom-webhook.service | Webhook 接收 | 9000 |
| zoom-api.service | REST API + 看板 | 8000 |

所有服务共享 `.env` 的 `EnvironmentFile`。

## DB 模型（含多租户）

- `tenants` — 租户
- `tenant_configs` — 租户配置（Zoom 凭证等）
- `monitored_meetings` — 被监控会议室
- `participant_events` — 参会事件（enter/leave，双源写入）
- `webhook_events` — Webhook 原始事件
- `seen_emails` — 陌生邮箱追踪
- `daily_stats` — 每日汇总
- `person_stats` — 每人每日统计
- `hourly_activity` — 逐时活跃度
- `alerts` — 告警记录

所有数据表均含 `tenant_id`。

## API 端点一览

| 路径 | 说明 |
|------|------|
| `GET /` | 服务信息 |
| `GET /api/v1/health` | 健康检查 + Telegram 联通性 |
| `GET /api/v1/dashboard/today` | 今日概览 |
| `GET /api/v1/dashboard/weekly` | 本周趋势 |
| `GET /api/v1/participants/ranking?days=30&limit=10` | 参会时长排行 |
| `GET /api/v1/alerts/recent?limit=20` | 最近预警 |
| `GET /api/v1/strangers` | 陌生邮箱列表 |
| `POST /api/v1/telegram/test` | 测试推送 |
| `POST /webhook/zoom` | Zoom Webhook 接收 |
| `/docs` | Swagger UI |

## 部署步骤

```bash
# 1. 安装依赖
cd /opt/zoom-monitor
./venv/bin/pip install -e ".[dev]"

# 2. 配置 .env
#    TELEGRAM_BOT_TOKEN / ZOOM_CLIENT_SECRET / ZOOM_WEBHOOK_SECRET 必须填

# 3. 初始化 DB
./venv/bin/python3 scripts/migrate_db.py

# 4. 启动服务（分别测试）
./venv/bin/python3 start_monitor.py      # 监控（前台）
./venv/bin/python3 start_webhook.py       # Webhook 端口9000
./venv/bin/python3 start_api.py           # API 端口8000

# 5. 写入 systemd + 开机自启
systemctl daemon-reload
systemctl enable zoom-monitor zoom-webhook zoom-api
systemctl restart zoom-monitor zoom-webhook zoom-api
```

## 商业规划

| 方案 | 限制 |
|------|------|
| Free | 1 Meeting |
| Pro | 10 Meetings |
| Business | 无限 + AI 分析 |

按会议室收费 / 按活跃人数收费。
核心市场：自习室 / 教培 / 线上课堂。

未来集成扩展：Google Meet, Teams, 腾讯会议, 飞书会议, Discord Stage, Telegram Voice Chat。

## Pitfalls

1. **旧进程不杀干净** — systemctl restart 不会 kill 旧 Python 进程，用 `kill -9` 手工杀
2. **.env 权限必须 600** — `chmod 600 /opt/zoom-monitor/.env`
3. **Token 为空** — `TELEGRAM_BOT_TOKEN` 必须在 BotFather 重新生成后写入
4. **Zoom Webhook 签名验证** — 如果 `ZOOM_WEBHOOK_SECRET` 为空，签名验证跳过（不安全，仅调试用）
5. **多租户初始** — 默认 tenant_id="default"，生产环境需创建独立租户
