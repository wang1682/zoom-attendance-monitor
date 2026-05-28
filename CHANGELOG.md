# Changelog

## v1.0.0-lite (2026-05-28)

### 特性亮点

- **双源参会监控** — Zoom API v2 轮询（30 秒间隔）+ Webhook 事件回调双通道，零漏录
- **Telegram 实时推送** — 参会进出、陌生人检测、签到提醒、迟到标记、静默模式
- **品牌化 Web 看板** — FastAPI + 暗色主题 + 品牌配色（通过 brand.json 全自定义）
- **AI 出勤报告** — 每日/每周自动化分析报告（DEEPSEEK/OpenAI API 驱动）
- **Telegram 指令 Bot** — `/status` `/enable` `/disable` `/quiet` `/loud` `/today` 全指令覆盖
- **推送时段控制** — `PUSH_START_HOUR` / `PUSH_END_HOUR` 避免非工作时间骚扰
- **签到截止控制** — `SIGNIN_DEADLINE_HOUR` 自动标记迟到
- **陌生人检测** — 邮箱去重 + 首现告警推送
- **推送模板引擎** — 全部文案通过 brand.json/yml 配置，无需改代码

### 架构

- systemd 四服务架构：api / webhook / monitor / command
- Docker Compose 四容器部署（非 root user，python:3.12-slim）
- SQLite 持久化（WAL 模式，并发安全），无需外部数据库
- Cloudflare Tunnel 兼容，端口 8000 (api) / 9000 (webhook)
- .env 密钥外置 + chmod 600 保护
- brand.json / brand.yml 品牌全自定义

### 安全审计（6/6 PASS）

| 审计项 | 结果 | 说明 |
|--------|------|------|
| 源码密钥扫描 | ✅ PASS | 全部 .py/.html/.md/.sh/.yml 无硬编码密钥 |
| 发布包密钥检查 | ✅ PASS | dist/tarball 不含任何真实密钥 |
| .env.example 安全 | ✅ PASS | 全部值为 placeholder/CHANGE_ME |
| 演示数据安全 | ✅ PASS | 20 个虚构用户名 + @example.com 邮箱 |
| install.sh 安全 | ✅ PASS | 无 chmod 777 / 无风险脚本 |
| 审计文档完整 | ✅ PASS | RELEASE_AUDIT.md 详细记录所有发现 |

Build hash: `8c66c8371d6c491ac6cf7499781239154a5aeffa866d7d6739df76a4540ecf1a`

### 工程改进

- 全部硬编码密钥移除，集中 .env 管理
- systemd EnvironmentFile 注入方式
- SQLite 3.46+ 兼容
- Alembic 数据库迁移框架
- 发布构建自动化脚本（build_release.sh + 密钥扫描验证）
- 备份/恢复脚本（backup_db.sh / restore_db.sh）
- 健康检查脚本（check_health.sh）
- 安全轮转检查脚本（rotate_secrets_check.sh）

### 管理后台

- RBAC 角色管理（admin / viewer / operator）
- 多租户支持（tenant isolation）
- 会议频道管理
- 账号配置管理
- 告警规则自定义
- 分析报告页面（daily / reports / risks）

### 依赖

- Python 3.10+
- FastAPI + uvicorn
- python-telegram-bot v21.x
- zoom-python-sdk
- SQLite 3.46+
- python-dotenv / pydantic
- （可选）Docker + Docker Compose
- （可选）DeepSeek / OpenAI API key for AI reports
