# Changelog

## v1.0.0-lite (2025-05-28)

### Features
- Zoom 会议室参会记录自动轮询（Polling + Webhook 双源）
- 参会进出与陌生人实时 Telegram 推送
- 品牌化 Web 看板（FastAPI + 暗色主题）
- Telegram 指令 Bot（/status /enable /disable /quiet /loud /today）
- 会员签到与超时提醒
- 推送模板引擎（通过 brand.json 配置全部文案）
- 陌生人检测（邮箱去重 + 首现告警）
- 推送时段控制（PUSH_START_HOUR / PUSH_END_HOUR）
- 签到截止时间控制（SIGNIN_DEADLINE_HOUR）

### Infrastructure
- systemd 四服务架构（api / webhook / monitor / command）
- Docker Compose 四容器部署
- SQLite 持久化（WAL 模式，并发安全）
- Cloudflare Tunnel 兼容
- .env 密钥外置（chmod 600）
- brand.json / brand.yml 品牌配置

### Dev
- 全部硬编码密钥移除，集中 .env 管理
- SQLite 3.46+ 兼容（DEFAULT CURRENT_TIMESTAMP）
- line-number strip 工具使用规范文档化
