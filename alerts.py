"""
alerts.py — Telegram 消息推送 (兼容层)

TelegramNotifier 内部使用 services.telegram.TelegramService。
保持类名和方法签名不变，避免 monitor.py 大改。
"""

from __future__ import annotations

import sys

from config import settings
from db import create_alert as _save_alert, log_alert_sent
from services.telegram import TelegramService


class TelegramNotifier:
    """兼容层：内部使用 TelegramService。保持 __init__(token=...) 签名。"""

    def __init__(self, token: str = ""):
        self._tg = TelegramService(token=token or settings.telegram_bot_token)

    def _classify(self, text: str) -> str:
        return self._tg._classify_static(text)

    async def send(self, text: str, chat_id: str = "", group: bool = False) -> bool:
        return self._tg.send_alert(text, chat_id=chat_id or "", group=group)

    async def health(self) -> bool:
        return await self._tg.health_async()
