# v1.7.4 — Per-Tenant Bot Token

**Breaking:** Telegram Bot 归属模型从 per-channel 重构为 per-tenant.

## 变更

- **tenants 表**新增 `telegram_bot_token` / `telegram_bot_username` / `telegram_bot_verified_at`
- **频道管理页**顶部新增 Bot 配置区：测试 Token → 保存（自动 `getMe` 验证）
- **`tenant_channels.bot_token`** 列废弃（保留供回滚，代码不再写入/读取）
- **`update_tenant_channel_bot_token`** 移除（无调用者）

## 升级

无需手动迁移 —— 启动时 `run_mt_migrations()` 自动执行 DDL。

## 兼容性

- 旧版 `tenant_channels.bot_token` 配置仍保留但已忽略
- 所有推送依赖 `tenants.telegram_bot_token` → 各租户独立 Bot

Commit: `7a02ef4`
