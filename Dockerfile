# ── Zoom Monitor: 单镜像多模式 ──────────────────────────────────────────────
# 构建方式：docker build -t zoom-monitor:latest .
# 运行方式：docker compose up -d

FROM python:3.12-slim AS base

WORKDIR /app

# 系统依赖（httpx、sqlite 等不需要额外系统包）
RUN apt-get update -qq && \
    apt-get install -y -qq --no-install-recommends curl ca-certificates && \
    rm -rf /var/lib/apt/lists/*

# 安装 Python 依赖
COPY requirements.prod.txt .
RUN pip install --no-cache-dir -r requirements.prod.txt

# ── 交付镜像 ────────────────────────────────────────────────────────────────
FROM base

COPY . .
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# 非 root 运行（减少 Container 攻击面）
RUN groupadd -r zoom && useradd -r -g zoom -d /app -s /sbin/nologin zoom && \
    chown -R zoom:zoom /app
USER zoom

# 健康检查（仅对 api/webhook 模式有意义，monitor/command 也兼容）
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD curl -sf http://localhost:8000/health || curl -sf http://localhost:9000/health || exit 1

ENTRYPOINT ["/entrypoint.sh"]
