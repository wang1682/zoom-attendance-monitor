# Zoom Monitor — Docker 部署

## 快速开始

```bash
# 1. 准备 .env（复制示例，填入真实密钥）
cp .env.example .env
nano .env

# 2. 构建镜像
docker compose build

# 3. 启动所有服务
docker compose up -d

# 4. 查看状态
docker compose ps
docker compose logs -f
```

## 服务

| 容器 | 端口 | 角色 |
|------|------|------|
| zoom-api | 8000 | 看板 + API |
| zoom-webhook | 9000 | Webhook 接收端 |
| zoom-monitor | — | 轮询监控 |

## 验证

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:9000/health
```

## 数据持久化

三个 Docker volume，分别对应：
- `zoom-data` → /app/data（tracking.db）
- `zoom-backups` → /app/backups
- `zoom-logs` → /app/logs

## 常用命令

```bash
# 启动
docker compose up -d

# 停止
docker compose down

# 停止并删除 volume（数据会丢！）
docker compose down -v

# 查看日志
docker compose logs -f

# 重启单个服务
docker compose restart zoom-api

# 重建镜像
docker compose build --no-cache

# 进入容器调试
docker compose exec zoom-api /bin/bash
# 或用 init shell 模式
CONTAINER_ROLE=shell docker compose run --rm zoom-api
```

## 注意

- 本部署是旁路模式，不影响主机现有 systemd `zoom-*.service`
- 端口只绑定 `127.0.0.1`，如需公网访问需配合 Cloudflare Tunnel
- 首次启动会自动建表（DB init 在 app.py 启动时幂等执行）
- 配置变更需重启容器：`docker compose restart`
