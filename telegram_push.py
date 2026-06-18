"""
telegram_push.py — Telegram 推送工具
send_message(chat_id, text) 发送消息到 Telegram
"""
from __future__ import annotations

import requests
from config import settings


def send_message(text: str, chat_id: str = None, bot_token: str = None, **kwargs) -> dict:
    """Send message to Telegram, return {ok, error, message_id}"""
    target = chat_id or settings.telegram_group_chat_id or settings.telegram_private_chat_id
    token = bot_token or settings.telegram_bot_token
    if not token or not target:
        return {"ok": False, "error": "Telegram not configured"}
    try:
        payload = {"chat_id": target, "text": text}
        payload.update(kwargs)
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=10
        )
        data = r.json()
        if data.get("ok"):
            msg_id = data.get("result", {}).get("message_id")
            return {"ok": True, "message_id": msg_id}
        return {"ok": False, "error": data.get("description", "unknown")}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def delete_message(chat_id: str, message_id: int, bot_token: str = None) -> bool:
    """
    Delete a Telegram message by chat_id and message_id.
    Returns True on success, logs warning on failure (never raises).
    """
    token = bot_token or settings.telegram_bot_token
    if not token or not chat_id or not message_id:
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{token}/deleteMessage",
            json={"chat_id": chat_id, "message_id": message_id},
            timeout=5
        )
        ok = r.json().get("ok", False)
        if ok:
            import logging
            logging.getLogger("telegram_push").info(
                f"deleteMessage success (chat={chat_id}, msg={message_id})"
            )
        else:
            import logging
            logging.getLogger("telegram_push").warning(
                f"deleteMessage failed (chat={chat_id}, msg={message_id}): {r.json().get('description', 'unknown')}"
            )
        return ok
    except Exception as e:
        import logging
        logging.getLogger("telegram_push").warning(
            f"deleteMessage exception (chat={chat_id}, msg={message_id}): {e}"
        )
        return False


# ═══════════════════════════════════════════
# Telegram 2FA
# ═══════════════════════════════════════════

_2fa_codes: dict[int, dict] = {}

def send_2fa_code(user_id: int, chat_id: str, bot_token: str = None) -> str | None:
    """
    Generate a 6-digit code, send via Telegram to the user's bound chat_id.
    Returns the code string on success, None on failure.
    Code is valid for 5 minutes, one-time use.
    """
    import secrets, time
    code = f"{secrets.randbelow(1000000):06d}"
    expires_at = time.time() + 300
    text = f"🔐 登录验证码\n\n{code}\n\n5分钟内有效，请勿泄露"
    try:
        result = send_message(text, chat_id, bot_token=bot_token)
        if not result.get("ok"):
            print(f"[2FA] Failed to send code to {chat_id}: {result.get('error')}")
            return None
        _2fa_codes[user_id] = {
            "code": code,
            "expires_at": expires_at,
            "chat_id": chat_id,
            "message_id": result.get("message_id"),
            "created_at": time.time(),
        }
        import logging
        logging.getLogger("telegram_push").info(
            f"send_2fa_code saved user_id={user_id} chat_id={chat_id} message_id={result.get('message_id')}"
        )
        return code
    except Exception as e:
        print(f"[2FA] Failed to send code to {chat_id}: {e}")
        return None

def verify_2fa_code(user_id: int, code: str) -> bool:
    """Verify a 6-digit 2FA code. Returns True if valid (and consumes it)."""
    import time
    entry = _2fa_codes.pop(user_id, None)
    if not entry:
        return False
    if time.time() > entry["expires_at"]:
        return False
    return entry["code"] == code

def get_2fa_entry(user_id: int) -> dict | None:
    """Return the _2fa_codes entry for a user, or None if not present."""
    return _2fa_codes.get(user_id)

def delete_2fa_message(user_id: int) -> None:
    """
    Delete the 2FA message for a user (called after successful verification or timeout).
    Never raises — failures are logged as warnings.
    """
    entry = _2fa_codes.get(user_id)
    if not entry:
        return
    chat_id = entry.get("chat_id")
    message_id = entry.get("message_id")
    if chat_id and message_id:
        delete_message(str(chat_id), message_id)
