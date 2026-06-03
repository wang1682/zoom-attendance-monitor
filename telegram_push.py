"""
telegram_push.py — Telegram 推送工具
send_message(chat_id, text) 发送消息到 Telegram
"""
from __future__ import annotations

import requests
from config import settings


def send_message(text: str, chat_id: str = None) -> dict:
    """Send message to Telegram, return {ok, error}"""
    target = chat_id or settings.telegram_group_chat_id or settings.telegram_private_chat_id
    token = settings.telegram_bot_token
    if not token or not target:
        return {"ok": False, "error": "Telegram not configured"}
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": target, "text": text},
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            return {"ok": True}
        return {"ok": False, "error": data.get("description", "unknown")}
    except Exception as e:
        return {"ok": False, "error": str(e)}
