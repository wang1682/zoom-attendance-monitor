# Configuration Reference — Zoom Attendance Monitor

## .env 文件

路径：`/opt/zoom-monitor/.env`
权限：`chmod 600`

### Zoom API 凭据

| 变量 | 必填 | 说明 | 获取方式 |
|------|------|------|---------|
| `ZOOM_ACCOUNT_ID` | 是 | Zoom 账户 ID | Zoom Marketplace → Server-to-Server OAuth App |
| `ZOOM_CLIENT_ID` | 是 | 应用 Client ID | 同上 |
| `ZOOM_CLIENT_SECRET` | 是 | 应用 Client Secret | 同上 |
| `ZOOM_HOST_EMAIL` | 是 | 主持人邮箱（用于过滤自己） | Zoom Profile |
| `ZOOM_PMI_ID` | 是 | 自习室 PMI 编号 | Zoom → Meetings → Personal Meeting ID |
| `ZOOM_EXTRA_MEETING_IDS` | 否 | 额外会议 ID（逗号分隔） | 手动添加 |

### Telegram 通知

| 变量 | 必填 | 说明 |
|------|------|------|
| `TELEGRAM_BOT_TOKEN` | 是 | BotFather 获取的 Bot Token |
| `TELEGRAM_PRIVATE_CHAT_ID` | 是 | 私聊推送目标 Chat ID |
| `TELEGRAM_GROUP_CHAT_ID` | 否 | 群聊推送目标 Chat ID |
| `TELEGRAM_GROUP_ENABLED` | 否 | 是否启用群组推送（默认 false） |

### Webhook

| 变量 | 必填 | 说明 |
|------|------|------|
| `ZOOM_WEBHOOK_SECRET` | 否 | Zoom Webhook Secret Token（用于签名验证）|

### 时段控制（MYT 吉隆坡时间）

| 变量 | 默认 | 说明 |
|------|------|------|
| `PUSH_START_HOUR` | 7 | 推送启用开始小时（0-23）|
| `PUSH_END_HOUR` | 23 | 推送启用结束小时（0-23）|
| `SIGNIN_DEADLINE_HOUR` | 9 | 签到截止小时（0-23）|

### 高级配置

| 变量 | 默认 | 说明 |
|------|------|------|
| `API_HOST` | 0.0.0.0 | API 监听地址 |
| `API_PORT` | 8000 | API 端口 |
| `WEBHOOK_HOST` | 0.0.0.0 | Webhook 监听地址 |
| `WEBHOOK_PORT` | 9000 | Webhook 端口 |
| `MONITOR_INTERVAL` | 25 | 轮询间隔（秒）|
| `DATABASE_URL` | sqlite:///data/tracking.db | 数据库路径 |

## 品牌配置（brand.json）

`brand.json` 控制 Web 看板外观和 Telegram 推送文案。

### 配色

```json
{
  "primary_color": "#0D6EFD",
  "bg_dark": "#0B0E14",
  "bg_card": "#1A1D24",
  "text_primary": "#FFFFFF",
  "text_secondary": "#9CA3AF"
}
```

### Telegram 模板

所有 14 个推送模板可在不修改代码的情况下自定义。支持变量注入：

- `{name}` — 参会人姓名
- `{email}` — 邮箱
- `{time}` — 时间
- `{count}` — 数量
- `{room}` — 会议室标签
- `{header_emoji}` — 品牌 emoji

## 数据库

- 引擎：SQLite（WAL 模式）
- 路径：`/opt/zoom-monitor/data/tracking.db`
- 无需额外数据库服务
- 备份：`scripts/backup_db.sh`
