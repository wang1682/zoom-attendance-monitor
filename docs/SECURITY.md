# Security Guide — Zoom Attendance Monitor

## Secrets Management

### .env 文件保护

```bash
chmod 600 /opt/zoom-monitor/.env          # 仅属主可读
chown root:root /opt/zoom-monitor/.env    # systemd 用 root 运行
```

- 绝对不要提交 `.env` 到 Git
- `.gitignore` 已包含 `.env`
- systemd 通过 `EnvironmentFile=/opt/zoom-monitor/.env` 注入

### 密钥清单

| 密钥 | 风险等级 | 说明 |
|------|---------|------|
| ZOOM_CLIENT_SECRET | 高 | 可代表你的 Zoom App 发起 API 请求 |
| ZOOM_ACCOUNT_ID | 中 | 账户标识，需配合 Client Secret 使用 |
| TELEGRAM_BOT_TOKEN | 高 | Token 泄露后任何人都可控制 Bot |
| ZOOM_WEBHOOK_SECRET | 中 | 用于验证 Webhook 来源真实性 |

### Secret Rotation 策略

- Zoom Client Secret：建议每 90 天轮换一次
- Telegram Bot Token：仅当泄露或需要强制重置时轮换（需通过 BotFather）
- Webhook Secret：可在 Zoom Marketplace 随时重新生成

## Network Security

### Port Exposure

| 端口 | 绑定 | 说明 |
|------|------|------|
| 8000 | 127.0.0.1 (推荐) | API 看板 — 仅本地访问 |
| 9000 | 0.0.0.0 (推荐) | Webhook — 需公网访问 |

**建议配置**：
- API 端口绑定 `127.0.0.1`，通过 Nginx 反代 + HTTPS 对外暴露
- Webhook 端口通过 Cloudflare Tunnel 暴露，不直接开公网

### TLS/HTTPS

```nginx
# Nginx 反代示例
server {
    listen 443 ssl;
    server_name zoom-monitor.your-domain.com;

    ssl_certificate /etc/letsencrypt/live/.../fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/.../privkey.pem;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    }
}
```

## Database Security

- SQLite 文件权限：`chmod 600 /opt/zoom-monitor/data/tracking.db`
- 存放路径：`/opt/zoom-monitor/data/`（不在 web 可访问目录）
- 定期备份：`scripts/backup_db.sh`
- 无 SQL 注入风险（使用参数化查询）

## Webhook Security

Webhook endpoint `/webhook` 支持签名验证：
- Zoom 发送请求时在 header 携带 `x-zm-signature`
- 服务端用 `ZOOM_WEBHOOK_SECRET` 做 HMAC-SHA256 验签
- 验证失败返回 403

> **注意**：如果 Webhook Secret 未配置，系统会跳过签名验证。建议始终配置。

## Monitoring & Auditing

- 所有 Telegram 推送写入 `alerts` 表（含触达状态）
- 所有参会进出记录写入 `zoom_participants` 表
- 所有 Webhook 事件原始 payload 写入 `zoom_events` 表
- systemd journal 日志：`journalctl -u zoom-monitor -f`

## Telegram Bot Security

- Bot 不会主动加入群聊，需人工添加
- Bot 不会处理来自非授权 Chat ID 的消息
- 私聊推送目标通过 `TELEGRAM_PRIVATE_CHAT_ID` 限定
- 群聊推送需要明确设置 `TELEGRAM_GROUP_ENABLED=true`

## Container Security (Docker)

- 非 root 用户运行（UID 1001）
- `.env` 不打包进镜像（`.dockerignore` 排除）
- 卷挂载数据持久化
- 最小化镜像（python:3.12-slim）
