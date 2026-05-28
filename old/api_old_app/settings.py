"""
settings.py — 配置中心，从 .env / 环境变量加载
使用 pydantic-settings 统一管理所有配置。
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(Path(__file__).parent.parent / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Zoom API ----
    zoom_account_id: str = ""
    zoom_client_id: str = ""
    zoom_client_secret: str = ""
    zoom_host_email: str = ""
    zoom_pmi_id: str = ""
    zoom_extra_meeting_ids: str = ""
    zoom_webhook_secret: str = ""

    # ---- Telegram ----
    telegram_bot_token: str = ""
    telegram_private_chat_id: str = "7922047310"
    telegram_group_chat_id: str = ""
    telegram_group_enabled: bool = False

    # ---- 时段控制 ----
    push_start_hour: int = 7
    push_end_hour: int = 23
    signin_deadline_hour: int = 9
    summary_hours: str = "16,23"
    daily_report_hour: int = 23
    overtight_minutes: int = 120

    # ---- DB ----
    database_url: str = "sqlite+aiosqlite:////opt/zoom-monitor/data/tracking.db"
    database_echo: bool = False

    # ---- Redis (optional) ----
    redis_url: str = ""

    # ---- App ----
    debug: bool = False
    secret_key: str = Field("", description="必填：session 签名密钥，不在 .env 设置则启动失败")
    default_tenant_id: str = "default"

    # ---- Dashboard Auth ----
    dashboard_admin_user: str = "admin"
    dashboard_admin_password_hash: str = ""

    # ---- API Token（未设置时只允许 cookie 登录态访问 API）----
    api_token: str = ""

    # ---- LLM API Keys ----
    deepseek_api_key: str = ""
    openai_api_key: str = ""
    sub2api_api_key: str = ""
    sub2api_base_url: str = "https://sub2api.dhbwang.xyz/v1"
    ai_report_model: str = "deepseek-chat"
    ai_fallback_model: str = "gpt-5.4-mini"

    # ---- 服务 ----
    monitor_interval: int = 30  # 轮询间隔（秒）
    webhook_port: int = 9000
    api_port: int = 8000

    @property
    def all_meeting_ids(self) -> list[str]:
        ids = []
        if self.zoom_pmi_id:
            ids.append(self.zoom_pmi_id)
        if self.zoom_extra_meeting_ids:
            ids.extend(x.strip() for x in self.zoom_extra_meeting_ids.split(",") if x.strip())
        return ids

    @property
    def summary_hour_list(self) -> list[int]:
        try:
            return [int(h) for h in self.summary_hours.split(",") if h.strip()]
        except (ValueError, AttributeError):
            return [16, 23]

    def validate_required(self):
        missing = []
        if not self.zoom_account_id:
            missing.append("ZOOM_ACCOUNT_ID")
        if not self.zoom_client_id:
            missing.append("ZOOM_CLIENT_ID")
        if not self.zoom_client_secret:
            missing.append("ZOOM_CLIENT_SECRET")
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.secret_key:
            missing.append("SECRET_KEY")
        if missing:
            raise RuntimeError(f"缺少必要配置: {', '.join(missing)}")


settings = Settings()
