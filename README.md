# Zoom Attendance Monitor

> AI-powered Zoom attendance tracking with Telegram alerts and automated reports.

---

## Features

- **Dashboard** — Real-time attendance dashboard with 1Panel-style UI
- **AI Reports** — Daily, weekly, and monthly AI-generated attendance reports
- **Telegram Alerts** — Instant push notifications for join/leave events
- **Webhook Integration** — Real-time Zoom event capture
- **Health Check** — One-click system health verification
- **Docker Deploy** — One-command deployment with Docker Compose

## Dashboard

Modern admin panel with:
- Real-time statistics cards
- Attendance trend charts
- Participant rankings
- AI summary widget
- Mobile-responsive layout

## AI Reports

Automated attendance analysis:
- AI Daily Report — today summary
- AI Weekly Report — 7-day analysis
- AI Monthly Report — 30-day trends
- Natural language summaries via GPT

## Telegram Alerts

Instant notifications:
- Participant joined/left
- Stranger detection
- Late arrival marking
- Attendance summaries

## Docker Deploy

```bash
git clone https://github.com/wang1682/zoom-attendance-monitor.git
cd zoom-attendance-monitor
cp .env.example .env
# Edit .env with your Zoom API credentials and Telegram bot token
docker compose up -d
```

## Quick Start

1. Create a Zoom Server-to-Server OAuth App
2. Create a Telegram bot via @BotFather
3. Configure .env with credentials
4. Run docker compose up -d
5. Open http://your-server:8082

## System Architecture

```
Zoom Meeting -> Webhook (9000) -> SQLite -> Dashboard (8082)
                    |-> Monitor (polling) ->|       |-> Telegram Bot
```

| Container | Role | Port |
|-----------|------|------|
| zoom-api | FastAPI Dashboard | 8082 |
| zoom-monitor | Zoom API polling | - |
| zoom-webhook | Zoom event receiver | 9000 |
| zoom-command | Telegram commands | - |

## Security

- All ports bind to 127.0.0.1 by default
- .env file must be chmod 600
- Example config fully sanitized
- Cloudflare Tunnel recommended

## License

MIT License 2026
