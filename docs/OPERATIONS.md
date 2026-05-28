# Operations Guide — Zoom Attendance Monitor

## Service Management

### systemd 命令

```bash
# 查看状态（全部四个服务）
sudo systemctl status zoom-{api,webhook,monitor,command}

# 启停控制
sudo systemctl restart zoom-api
sudo systemctl stop zoom-monitor
sudo systemctl start zoom-command

# 开机自启
sudo systemctl enable zoom-api
sudo systemctl disable zoom-monitor  # 临时关闭开机自启
```

### Docker 命令

```bash
cd /opt/zoom-monitor

# 启动全部
docker compose up -d

# 停止全部
docker compose down

# 重建
docker compose build --no-cache && docker compose up -d

# 查看日志
docker compose logs -f --tail 50
docker logs zoom-monitor -f --tail 50
```

## Logging

### systemd journal

```bash
# 实时日志
journalctl -u zoom-monitor -f -n 50

# 查看指定时间范围
journalctl -u zoom-api --since "2025-05-28 00:00" --until "2025-05-28 23:59"

# 按严重级别过滤
journalctl -u zoom-api -p err
```

### Docker 日志

```bash
docker logs zoom-api --tail 100
docker logs zoom-webhook -f --tail 20
```

## Database Operations

### 备份

```bash
scripts/backup_db.sh
# 默认备份到 /opt/zoom-monitor/backups/
```

### 恢复

```bash
scripts/restore_db.sh /opt/zoom-monitor/backups/zoom-monitor-2025-05-28.sqlite
```

### 手动检查

```bash
sqlite3 /opt/zoom-monitor/data/tracking.db "SELECT COUNT(*) FROM zoom_participants;"
sqlite3 /opt/zoom-monitor/data/tracking.db "SELECT * FROM zoom_participants ORDER BY id DESC LIMIT 5;"
sqlite3 /opt/zoom-monitor/data/tracking.db "SELECT * FROM zoom_events ORDER BY id DESC LIMIT 3;"
sqlite3 /opt/zoom-monitor/data/tracking.db "SELECT * FROM alerts ORDER BY id DESC LIMIT 5;"
```

## Health Checks

```bash
# 脚本检查
scripts/check_health.sh

# 手动检查
curl -sf http://127.0.0.1:8000/health && echo "api OK" || echo "api FAIL"
curl -sf http://127.0.0.1:9000/health && echo "webhook OK" || echo "webhook FAIL"
```

## Updating

```bash
# 1. 备份
scripts/backup_db.sh

# 2. 停止服务
sudo systemctl stop zoom-{api,webhook,monitor,command}

# 3. 更新代码
# git pull / 解压新版本

# 4. 更新依赖
source venv/bin/activate
pip install -r requirements.txt --upgrade

# 5. 启动
sudo systemctl start zoom-{api,webhook,monitor,command}

# 6. 验证
scripts/check_health.sh
```

## Troubleshooting

### 轮询无输出

检查 Telegram Bot Token 和 Zoom API 凭据：
```bash
curl -s "https://api.telegram.org/bot$(grep TELEGRAM_BOT_TOKEN .env | cut -d= -f2)/getMe"
grep -E "ZOOM_ACCOUNT|ZOOM_CLIENT" .env
```

### Webhook 收不到

1. 检查端口监听：`ss -tlnp | grep 9000`
2. 检查公网可达：从外网 `curl https://your-domain.com/health`
3. Zoom Marketplace → Webhook → 查看 Delivery Log

### 服务不断重启

```bash
journalctl -u zoom-monitor -n 100 --no-pager | grep -i error
# 常见原因：.env 缺失，Python 依赖缺失，权限问题
```

### 磁盘空间

```bash
du -sh /opt/zoom-monitor/data/tracking.db
du -sh /opt/zoom-monitor/backups/
```
