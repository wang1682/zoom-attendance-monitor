# Install Guide — Zoom Attendance Monitor

## System Requirements

- Linux (Ubuntu 22.04+ / Debian 12+ recommended)
- Python 3.10+
- 512 MB+ RAM
- 1 GB+ free disk
- Zoom 账户（Pro 或 Business，支持 Server-to-Server OAuth）

## Quick Install

```bash
# 1. 项目目录
sudo mkdir -p /opt/zoom-monitor
sudo chown $USER:$USER /opt/zoom-monitor
cd /opt/zoom-monitor

# 2. 获取代码
# 方式 A: git clone
# git clone https://github.com/your-org/zoom-attendance-monitor.git .
# 方式 B: 解压发布包
# unzip zoom-monitor-v1.0.0-lite.zip -d /opt/zoom-monitor

# 3. 创建 Python 虚拟环境
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

# 4. 配置 .env
cp .env.example .env
chmod 600 .env
# 编辑 .env — 填入 Zoom 凭据和 Telegram Token

# 5. 初始化数据库（自动，启动后生成 tracking.db）

# 6. 安装 systemd 服务
# 编辑 service 文件中的 ExecStart 路径，指向你的 python3 和项目路径
```

## Step-by-Step

### Step 1: Zoom Marketplace App

1. 登录 https://marketplace.zoom.us → Develop → Build App → Server-to-Server OAuth
2. 添加以下 Scopes:
   - `meeting:read:participant` — 拉参会列表
   - `meeting:read:webinar_participant` — 如果会用网络研讨会
   - `meeting:read:list_meetings` — 获取排期会议
   - `webhook:read:participant` — Webhook 接收进出事件
3. 记下 Account ID, Client ID, Client Secret
4. 如果启用 Webhook：添加 Webhook endpoint `https://your-domain.com/webhook`，记下 Secret Token

### Step 2: Telegram Bot

1. 找 @BotFather → `/newbot` → 取名 → 得到 Token
2. 找 @userinfobot → `/start` → 得到你的 Chat ID（私聊目标）
3. （可选）建一个群，把 Bot 拉入群，用 @userinfobot 获取群 Chat ID

### Step 3: 配置 .env

```ini
ZOOM_ACCOUNT_ID=
ZOOM_CLIENT_ID=
ZOOM_CLIENT_SECRET=
ZOOM_HOST_EMAIL=your-email@example.com
ZOOM_PMI_ID=your-pmi-id  # 替换为你的 PMI

TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_PRIVATE_CHAT_ID=your-chat-id  # 替换为你的 Telegram Chat ID
TELEGRAM_GROUP_ENABLED=false

ZOOM_WEBHOOK_SECRET=
PUSH_START_HOUR=7
PUSH_END_HOUR=23
SIGNIN_DEADLINE_HOUR=9
```

### Step 4: 启动服务

#### systemd（推荐生产）

```bash
# 安装服务
sudo cp systemd/zoom-*.service /etc/systemd/system/
# 编辑 ExecStart 路径

sudo systemctl daemon-reload
sudo systemctl enable --now zoom-api zoom-webhook zoom-monitor zoom-command
sudo systemctl status zoom-*
```

#### Docker Compose

```bash
docker compose build
docker compose up -d
docker compose ps
```

### Step 5: 验证

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:9000/health
ls -la tracking.db  # 确认自动创建
```

### Step 6: Webhook（可选）

1. 确保 Webhook 服务可公网访问（Cloudflare Tunnel / Nginx 反代）
2. Zoom Marketplace → Feature → Add Webhook Subscription
3. Event types: `Meeting Participant Joined`, `Meeting Participant Left`
4. Endpoint URL: `https://your-domain.com/webhook`

## Firewall

```bash
# 如果使用 iptables
sudo iptables -A INPUT -p tcp --dport 8000 -s 127.0.0.1 -j ACCEPT  # API 仅本地
sudo iptables -A INPUT -p tcp --dport 9000 -j ACCEPT               # Webhook 公开
```
