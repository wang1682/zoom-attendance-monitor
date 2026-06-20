"""
telegram_push.py — Telegram 推送工具 (兼容层)

All functions delegate to services/telegram.TelegramService internally.
旧代码保持 from telegram_push import send_message 不变。
新代码应直接使用 services.telegram.TelegramService。
"""

from __future__ import annotations

from services.telegram import TelegramService


def send_message(text: str, chat_id: str = None, bot_token: str = None, **kwargs) -> dict:
    """发送消息到 Telegram。兼容旧签名，返回 {ok, error, message_id}"""
    tg = TelegramService(token=bot_token or "", chat_id=chat_id or "")
    return tg.send(text, chat_id=chat_id or "", **kwargs)


def delete_message(chat_id: str, message_id: int, bot_token: str = None) -> bool:
    """删除 Telegram 消息"""
    return TelegramService.delete_message(chat_id, message_id, bot_token=bot_token)


# ═══════════════════════════════════════════
# Telegram 2FA (兼容层)
# ═══════════════════════════════════════════

def send_2fa_code(user_id: int, chat_id: str, bot_token: str = None) -> str | None:
    """生成 6 位验证码并发送"""
    return TelegramService.send_2fa_code(user_id, chat_id, bot_token=bot_token)


def verify_2fa_code(user_id: int, code: str) -> bool:
    """验证 2FA 验证码（一次性）"""
    return TelegramService.verify_2fa_code(user_id, code)


def get_2fa_entry(user_id: int) -> dict | None:
    """获取 2FA 条目（用于检查是否已发送）"""
    return TelegramService.get_2fa_entry(user_id)


def delete_2fa_message(user_id: int) -> None:
    """删除已发送的 2FA 消息"""
    TelegramService.delete_2fa_message(user_id)
