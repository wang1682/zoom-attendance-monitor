# Zoom Attendance Monitor

> AI-powered Zoom attendance tracking with Telegram alerts and automated reports.

---

<p align="center">
  <img src="docs/screenshots/dashboard.png" alt="Dashboard" width="800">
  <br>
  <em>实时仪表盘 · 1Panel 风格界面 · AI 摘要</em>
</p>

## Demo

<p align="center">
  <img src="docs/demo.gif" alt="Demo" width="800">
  <br>
  <em>Dashboard → 参会明细 → 报表分析 → AI 报告 → 系统设置（15 秒）</em>
</p>

---

## Features

| | |
|---|---|
| Dashboard | 1Panel-style UI with real-time cards, trend charts, participant rankings |
| AI Reports | Daily, weekly, monthly AI-generated attendance summaries |
| Telegram Alerts | Instant push for join/leave, stranger detection, late marking |
| Webhook | Real-time Zoom event capture via webhook + polling dual channel |
| Health Check | One-click verify Zoom API, Webhook, Telegram, AI, Database |
| Docker | One-command docker compose up -d deployment |

## Screenshots

| Dashboard | Participants |
|---|---|
| ![Dashboard](docs/screenshots/dashboard.png) | ![Participants](docs/screenshots/participants.png) |
| Reports | AI Report |
| ![Reports](docs/screenshots/reports.png) | ![AI Report](docs/screenshots/ai-report.png) |
| Settings | Telegram Push |
| ![Settings](docs/screenshots/settings.png) | ![Telegram](docs/screenshots/telegram-push.png) |

## Quick Start

```
git clone https://github.com/wang1682/zoom-attendance-monitor.git
cd zoom-attendance-monitor
cp .env.example .env
# Edit .env with your Zoom API credentials and Telegram bot token
docker compose up -d
```

Open http://your-server:8082

## Architecture

```
Zoom Meeting -> Webhook (9000) -> SQLite -> Dashboard (8082)
                    |-> Monitor (polling) ->|       |-> Telegram Bot
```

| Container | Role | Port |
|-----------|------|------|
| zoom-api | FastAPI Dashboard + API | 8082 |
| zoom-monitor | Zoom API polling | - |
| zoom-webhook | Zoom event receiver | 9000 |
| zoom-command | Telegram command handler | - |

## Security

- All ports bind to 127.0.0.1 by default
- .env file chmod 600
- Example config fully sanitized
- Cloudflare Tunnel recommended

## License

MIT License 2026
