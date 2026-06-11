"""
config.py — 从 .env / 环境变量加载所有配置
轻量版：不用 pydantic_settings，避免 systemd 环境缺依赖
"""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

BASE = Path(__file__).parent
load_dotenv(BASE / ".env")


def _env(key: str, default: str | None = None) -> str:
    return os.environ.get(key, default or "")


def _int(key: str, default: int) -> int:
    try:
        return int(_env(key, str(default)))
    except (ValueError, TypeError):
        return default


def _bool(key: str) -> bool:
    return _env(key, "false").lower() in ("1", "true", "yes")


class Settings:
    # Demo 模式
    demo_mode: bool = _bool("DEMO_MODE")

    # Zoom API
    zoom_account_id: str = _env("ZOOM_ACCOUNT_ID")
    zoom_client_id: str = _env("ZOOM_CLIENT_ID")
    zoom_client_secret: str = _env("ZOOM_CLIENT_SECRET")
    zoom_host_email: str = _env("ZOOM_HOST_EMAIL")
    zoom_pmi_id: str = _env("ZOOM_PMI_ID")
    zoom_extra_meeting_ids: str = _env("ZOOM_EXTRA_MEETING_IDS")
    zoom_webhook_secret: str = _env("ZOOM_WEBHOOK_SECRET")

    # Telegram
    telegram_bot_token: str = _env("TELEGRAM_BOT_TOKEN")
    telegram_private_chat_id: str = _env("TELEGRAM_PRIVATE_CHAT_ID")
    telegram_group_chat_id: str = _env("TELEGRAM_GROUP_CHAT_ID")
    telegram_group_enabled: bool = _bool("TELEGRAM_GROUP_ENABLED")
    telegram_group2_chat_id: str = _env("TELEGRAM_GROUP2_CHAT_ID")
    telegram_group2_enabled: bool = _bool("TELEGRAM_GROUP2_ENABLED")
    telegram_group2_report_interval: int = _int("TELEGRAM_GROUP2_REPORT_INTERVAL", 32400)

    # 时段（所有小时值均为 MYT UTC+8）
    push_start_hour: int = _int("PUSH_START_HOUR", 0)         # MYT — 0=全天
    push_end_hour: int = _int("PUSH_END_HOUR", 0)            # MYT — 0=全天
    signin_deadline_hour: int = _int("SIGNIN_DEADLINE_HOUR", 9)  # MYT 9AM = UTC 1AM
    summary_hours: str = _env("SUMMARY_HOURS", "16,23")       # MYT
    daily_report_hour: int = _int("DAILY_REPORT_HOUR", 23)    # MYT
    overtime_minutes: int = _int("OVERTIME_MINUTES", 120)

    # DB
    database_url: str = _env("DATABASE_URL", "sqlite:////opt/zoom-monitor/data/tracking.db")

    # App
    debug: bool = _bool("DEBUG")
    secret_key: str = _env("SECRET_KEY")
    session_secret: str = _env("SESSION_SECRET", _env("SECRET_KEY"))
    api_token: str = _env("API_TOKEN")
    api_port: int = _int("API_PORT", 8000)
    api_host: str = _env("API_HOST", "0.0.0.0")
    webhook_port: int = _int("WEBHOOK_PORT", 9000)
    webhook_host: str = _env("WEBHOOK_HOST", "0.0.0.0")
    monitor_interval: int = _int("MONITOR_INTERVAL", 30)

    # LLM
    deepseek_api_key: str = _env("DEEPSEEK_API_KEY")
    deepseek_base_url: str = _env("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
    ai_report_model: str = _env("AI_REPORT_MODEL", "deepseek-chat")

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
        if self.demo_mode:
            return  # Demo 模式不需要真实凭据
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
