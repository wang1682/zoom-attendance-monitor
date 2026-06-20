"""
command_bot.py — Telegram 指令处理
独立长轮询进程，通过 SQLite bot_state 控制 push_enabled / quiet_mode
仅响应 TELEGRAM_PRIVATE_CHAT_ID 的白名单指令
"""

from __future__ import annotations

import time
import logging
import requests

from config import settings
from db import (
    init_bot_state,
    get_bot_state,
    set_bot_state,
    log_command,
)

logger = logging.getLogger(__name__)

API_BASE = f"https://api.telegram.org/bot{settings.telegram_bot_token}"
GETUPDATES_URL = f"{API_BASE}/getUpdates"
PRIVATE_CHAT = settings.telegram_private_chat_id
POLL_TIMEOUT = 30  # long-poll 秒


def _send(chat_id: str, text: str, parse_mode: str = "HTML") -> bool:
    try:
        from services.telegram import TelegramService
        tg = TelegramService(chat_id=chat_id)
        result = tg.send(text, parse_mode=parse_mode)
        return result.get("ok", False)
    except Exception as e:
        logger.warning("send_message 失败: %s", e)
        return False


def _reply(chat_id: str, text: str):
    """仅回复私聊白名单"""
    if chat_id != PRIVATE_CHAT:
        return
    _send(chat_id, text)


def _status_text() -> str:
    push = "on" if get_bot_state("push_enabled", "1") == "1" else "off"
    quiet = "on" if get_bot_state("quiet_mode", "0") == "1" else "off"
    return (
        f"── <b>Zoom Monitor 状态</b> ──\n\n"
        f"push_enabled: <b>{push}</b>\n"
        f"quiet_mode:   <b>{quiet}</b>\n\n"
        f"可用指令: /help"
    )


def _help_text() -> str:
    return (
        "<b>Zoom Monitor 指令</b>\n\n"
        "/status — 当前推送状态\n"
        "/mute — 关闭推送\n"
        "/unmute — 开启推送\n"
        "/quiet_on — 静默模式（仅关键推送）\n"
        "/quiet_off — 关闭静默模式\n"
        "/help — 显示此帮助\n\n"
        "状态存储在 SQLite，重启不丢失。"
    )


def handle_command(chat_id: str, cmd: str, args: str = ""):
    """处理单条指令"""
    if chat_id != PRIVATE_CHAT:
        logger.info("非白名单 chat_id=%s 被忽略", chat_id)
        return

    cmd = cmd.lower()

    if cmd == "/status":
        resp = _status_text()
    elif cmd == "/mute":
        set_bot_state("push_enabled", "0")
        resp = "推送已关闭。"
    elif cmd == "/unmute":
        set_bot_state("push_enabled", "1")
        resp = "推送已开启。"
    elif cmd == "/quiet_on":
        set_bot_state("quiet_mode", "1")
        resp = "静默模式已开启（仅关键推送）。"
    elif cmd == "/quiet_off":
        set_bot_state("quiet_mode", "0")
        resp = "静默模式已关闭。"
    elif cmd == "/help":
        resp = _help_text()
    else:
        resp = f"未知指令: {cmd}\n可用指令: /help"

    _reply(chat_id, resp)
    log_command(chat_id, cmd, args, resp)
    logger.info("指令 %s → %s", cmd, resp[:60])


def poll_loop():
    """长轮询 Telegram getUpdates"""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s %(message)s",
    )
    logger.info("CommandBot 启动, 白名单 chat_id=%s", PRIVATE_CHAT)
    init_bot_state()
    offset = 0

    while True:
        try:
            resp = requests.get(GETUPDATES_URL,
                params={
                    "offset": offset,
                    "timeout": POLL_TIMEOUT,
                    "allowed_updates": ["message"],
                },
                timeout=POLL_TIMEOUT + 5,
            )
            if not resp.ok:
                logger.warning("getUpdates HTTP %s", resp.status_code)
                time.sleep(5)
                continue

            data = resp.json()
            if not data.get("ok"):
                logger.warning("getUpdates not ok: %s", data.get("description"))
                time.sleep(5)
                continue

            for update in data.get("result", []):
                offset = update["update_id"] + 1
                msg = update.get("message")
                if not msg:
                    continue

                chat_id = str(msg["chat"]["id"])
                text = (msg.get("text") or "").strip()

                if text.startswith("/"):
                    parts = text.split(maxsplit=1)
                    cmd = parts[0]
                    args = parts[1] if len(parts) > 1 else ""
                    handle_command(chat_id, cmd, args)

        except requests.Timeout:
            continue  # long-poll 正常超时，继续
        except Exception as e:
            logger.error("poll_loop 异常: %s", e)
            time.sleep(5)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s %(message)s")
    poll_loop()
