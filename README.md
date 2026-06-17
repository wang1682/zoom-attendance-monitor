# Zoom Attendance Monitor

> AI-powered Zoom attendance tracking with Telegram alerts and automated reports.

<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="Dashboard" width="800">
</p>

## Demo

<p align="center">
  <img src="docs/demo.gif" alt="Demo" width="800">
</p>

## Try Without Zoom

Try the full dashboard without any Zoom account or Telegram bot:

bash
DEMO_MODE=true docker compose up -d

Open http://localhost:8082/demo

Mock data auto-generated. No credentials needed.

## Quick Start

bash
git clone https://github.com/wang1682/zoom-attendance-monitor.git
cd zoom-attendance-monitor
cp .env.example .env
docker compose up -d

Open http://localhost:8082

## Features

- Dashboard - 1Panel-style UI with real-time cards, trends, rankings
- AI Reports - Daily, weekly, monthly AI-generated summaries
- Telegram Alerts - Instant push for join/leave, stranger detection
- Webhook - Real-time Zoom event capture via webhook + polling
- Health Check - One-click verify Zoom API, Webhook, Telegram, AI
- Docker - One-command deployment

## Screenshots

| Dashboard | Participants |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Participants](docs/screenshots/participants.png) |
| Reports | AI Report |
| ![Reports](docs/screenshots/reports.png) | ![AI Report](docs/screenshots/ai-report.png) |
| Settings | Telegram Push |
| ![Settings](docs/screenshots/settings.png) | ![Telegram](docs/screenshots/telegram-push.png) |

## Architecture

Zoom Meeting -> Webhook (9000) -> SQLite -> Dashboard (8082)
                    |-> Monitor (polling) ->|       |-> Telegram Bot

| Container | Role | Port |
|-----------|------|------|
| zoom-api | FastAPI Dashboard | 8082 |
| zoom-monitor | Zoom API polling | - |
| zoom-webhook | Zoom event receiver | 9000 |
| zoom-command | Telegram command handler | - |

## Security

- All ports bind to 127.0.0.1 by default
- .env file chmod 600
- Example config fully sanitized
- Cloudflare Tunnel recommended

## Ops

### 跨租户 group_id 完整性检查

检测 `member_display.group_id` 是否指向了错误租户的分组。

```bash
# 容器内运行
docker compose exec -T zoom-api python3 /app/scripts/check_member_group_tenant_integrity.py

# 或先用 docker cp 同步
docker cp scripts/check_member_group_tenant_integrity.py zoom-api:/app/scripts/
docker exec zoom-api python3 /app/scripts/check_member_group_tenant_integrity.py
```

**返回码**：
- `0` — 无脏数据
- `2` — 检测到跨租户分组绑定（输出见 stdout）

**自动修复（生产谨慎使用）**：
当同一租户下存在同名分组时，可安全迁移：

```bash
docker exec zoom-api python3 /app/scripts/check_member_group_tenant_integrity.py --fix
```

`--fix` 模式仅在 `member_groups` 中存在与当前分组同名的同租户记录时，
才将 `group_id` 迁移到正确值。非同名的跨租户绑定（手动废弃的分组）不会被自动处理。

### Cron 推荐配置

```cron
# 每天 10:00 检查，有脏数据则推送给管理员
0 10 * * * cd /path/to/project && \
  docker compose exec -T zoom-api python3 /app/scripts/check_member_group_tenant_integrity.py \
  2>&1 | mail -s "[Zoom Monitor] 分组完整性告警" admin@example.com
```

## License

MIT License 2026
