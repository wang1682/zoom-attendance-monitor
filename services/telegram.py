"""
services/telegram.py — Unified Telegram notification service.

Phase 2: Consolidates all Telegram push logic into a single Service class.

Two construction modes:
  1. TelegramService(request)          — uses session context for bot/chat_id
  2. TelegramService(token="...", chat_id="...")  — explicit (monitor/background)

Public API:
  service.send(text, ...)             — send plain text message
  service.send_markdown(text, ...)    — send with Markdown parse_mode
  service.send_alert(text, group=True) — monitor-style push with quiet_mode check
  service.test_channel(name, chat_id) — test a channel
  service.health() -> bool            — check bot token
  service.classify(text) -> str       — classify message type

Static / 2FA (unchanged from telegram_push.py legacy):
  TelegramService.send_2fa_code(user_id, chat_id, bot_token=None) -> str|None
  TelegramService.verify_2fa_code(user_id, code) -> bool
  TelegramService.get_2fa_entry(user_id) -> dict|None
  TelegramService.delete_2fa_message(user_id) -> None
  TelegramService.delete_message(chat_id, msg_id, bot_token=None) -> bool
"""

from __future__ import annotations

import secrets
import sys
import time
from typing import TYPE_CHECKING

import httpx
import requests
from config import settings

if TYPE_CHECKING:
    from fastapi import Request

from services.base import BaseService

# ════════════════════════════════════════════════════════════════
# 2FA (in-memory — stays in this module, survives across instances)
# ════════════════════════════════════════════════════════════════
_2fa_codes: dict[int, dict] = {}


class TelegramService(BaseService):
    """Unified Telegram notification service.

    Usage::

        from services.telegram import TelegramService

        # In route handlers (FastAPI request available):
        tg = TelegramService(request)
        tg.send("Hello from Zoom Monitor")

        # In background tasks / monitor (explicit token):
        tg = TelegramService(token="123:abc", chat_id="-1001234")
        tg.send_alert("Warning: stranger detected", group=True)

        # Static 2FA methods:
        TelegramService.send_2fa_code(user_id, chat_id)
        TelegramService.verify_2fa_code(user_id, code)
    """

    def __init__(
        self,
        request: Request | None = None,
        *,
        token: str = "",
        chat_id: str = "",
    ):
        super().__init__(request)

        # If request is given, try to extract channel info from it later.
        # If explicit token/chat_id are given, use those directly.
        self._token = token or settings.telegram_bot_token
        self._chat_id = chat_id or ""

        # Cache for async client
        self._client: httpx.AsyncClient | None = None

    # ── Properties ──────────────────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        return f"https://api.telegram.org/bot{self._token}"

    # ── Send helpers ────────────────────────────────────────────────────────

    def send(
        self,
        text: str,
        chat_id: str = "",
        parse_mode: str = "",
        **kwargs,
    ) -> dict:
        """Synchronous send message. Returns dict with 'ok', 'message_id', 'error'.

        Falls back to request context or instance chat_id if not specified.
        """
        _chat_id = chat_id or self._chat_id or self._resolve_chat_id()
        return self._do_send(text, _chat_id, parse_mode, **kwargs)

    def send_markdown(self, text: str, chat_id: str = "", **kwargs) -> dict:
        """Send with MarkdownV2 parse_mode (escapes automatically)."""
        return self.send(text, chat_id, parse_mode="MarkdownV2", **kwargs)

    def send_alert(
        self,
        text: str,
        chat_id: str = "",
        group: bool = False,
    ) -> bool:
        """Monitor-style push: respects push_enabled / quiet_mode.
        Returns True on success.
        """
        # ── push_enabled / quiet_mode ──
        from db import is_push_enabled, is_quiet_mode

        if not is_push_enabled():
            sys.stdout.write("[ALERTS] push_enabled=off, 跳过推送\n")
            sys.stdout.flush()
            return False

        if is_quiet_mode():
            mtype = self._classify_static(text)
            critical_types = {"stranger"}
            if mtype not in critical_types:
                sys.stdout.write(f"[ALERTS] quiet_mode=on, 跳过非关键推送 ({mtype})\n")
                sys.stdout.flush()
                return False

        # ── Build targets ──
        targets = [chat_id] if chat_id else [self._chat_id or settings.telegram_private_chat_id]
        if group and settings.telegram_group_enabled and settings.telegram_group_chat_id:
            targets.append(settings.telegram_group_chat_id)
        if group and settings.telegram_group2_enabled and settings.telegram_group2_chat_id:
            targets.append(settings.telegram_group2_chat_id)

        # ── Log alert ──
        from db import create_alert as _save_alert, log_alert_sent

        mtype = self._classify_static(text)
        title = text.split("\n")[0] if text else "推送"
        alert_id = _save_alert(
            alert_type=mtype,
            title=title.strip(" *"),
            message=text[:500],
        )

        success = True
        for cid in targets:
            if not cid:
                continue
            result = self._do_send_sync_retry(text, cid)
            if not result.get("ok"):
                success = False

        log_alert_sent(alert_id, ",".join(filter(None, targets)), success)
        return success

    def health(self) -> bool:
        """Check if the bot token is valid."""
        if not self._token:
            return False
        try:
            r = requests.get(f"{self.base_url}/getMe", timeout=5)
            return r.status_code == 200 and r.json().get("ok")
        except Exception:
            return False

    # ── Test channel ────────────────────────────────────────────────────────

    def test_channel(self, name: str, chat_id: str) -> bool:
        """Send a test message to a channel. Returns True on success."""
        msg = (
            f"✅ 这是一条测试消息\n\n"
            f"频道：{name}\n"
            f"ID：{chat_id}\n\n"
            f"如果收到此消息，说明 Telegram 通知配置正确。"
        )
        result = self.send(msg, chat_id=chat_id)
        return result.get("ok", False)

    # ── Async helpers ──────────────────────────────────────────────────────

    async def send_async(self, text: str, chat_id: str = "", **kwargs) -> dict:
        """Async send (for use inside async route handlers)."""
        _chat_id = chat_id or self._chat_id or self._resolve_chat_id()
        return await self._do_send_async(text, _chat_id, **kwargs)

    async def health_async(self) -> bool:
        """Async health check (avoids thread pool)."""
        if not self._token:
            return False
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                r = await client.get(f"{self.base_url}/getMe")
                return r.status_code == 200 and r.json().get("ok")
            except Exception:
                return False

    # ── Classification ─────────────────────────────────────────────────────

    @staticmethod
    def _classify_static(text: str) -> str:
        """Classify message type from text content."""
        if "陌生" in text or "新面孔" in text:
            return "stranger"
        if "迟到" in text or "超时" in text or "未签到" in text:
            return "overtime"
        if "日报" in text or "每日" in text or "📊" in text:
            return "daily"
        if "汇总" in text or "离开" in text or "📋" in text:
            return "summary"
        if "结束" in text or "开始" in text or "🎬" in text or "🔚" in text:
            return "meeting_event"
        return "event"

    # ── Internal ────────────────────────────────────────────────────────────

    def _resolve_chat_id(self) -> str:
        """Try to get chat_id from request context or settings."""
        # 1: Try session's telegram_chat_id if available
        if self.context and self.request:
            from db import get_user_by_id
            user = get_user_by_id(self.context.user_id)
            if user and user.get("telegram_chat_id"):
                return str(user["telegram_chat_id"])
        # 2: Fallback to settings
        return settings.telegram_private_chat_id or ""

    def _do_send(self, text: str, chat_id: str, parse_mode: str = "", **kwargs) -> dict:
        """Synchronous sendMessage via requests."""
        if not self._token or not chat_id:
            if not chat_id:
                return {"ok": False, "error": "no chat_id"}
            return {"ok": False, "error": "Telegram not configured"}
        try:
            payload = {"chat_id": chat_id, "text": text}
            if parse_mode:
                payload["parse_mode"] = parse_mode
            payload.update(kwargs)
            r = requests.post(
                f"{self.base_url}/sendMessage",
                json=payload,
                timeout=10,
            )
            data = r.json()
            if data.get("ok"):
                msg_id = data.get("result", {}).get("message_id")
                return {"ok": True, "message_id": msg_id}
            return {"ok": False, "error": data.get("description", "unknown")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _do_send_sync_retry(self, text: str, chat_id: str) -> dict:
        """Send with print-debug style (for monitor.py compatibility).
        已在 async context 中时走 create_task，否则 asyncio.run。
        """
        import asyncio
        try:
            loop = asyncio.get_running_loop()
            # 已有 event loop → create_task + run_until_complete
            # 不能直接在本 loop 上 run_until_complete（会阻塞），用 run_coroutine_threadsafe
            import httpx
            async def _send_inner():
                try:
                    async with httpx.AsyncClient(timeout=10) as cl:
                        r = await cl.post(
                            f"{self.base_url}/sendMessage",
                            json={"chat_id": chat_id, "text": text},
                        )
                        if r.status_code != 200:
                            sys.stderr.write(f"[TG ERROR] {chat_id}: {r.status_code} {r.text[:100]}\n")
                            return {"ok": False, "error": r.text[:100]}
                        return {"ok": True}
                except Exception as e:
                    sys.stderr.write(f"[TG EXCEPTION] {chat_id}: {e}\n")
                    return {"ok": False, "error": str(e)}
            fut = asyncio.run_coroutine_threadsafe(_send_inner(), loop)
            result = fut.result(timeout=15)
            if not result.get("ok"):
                sys.stderr.write(f"[TG ERROR] {chat_id}: {result.get('error','')}\n")
            return result
        except RuntimeError:
            # 无 event loop → asyncio.run (原始方案)
            import httpx
            async def _send():
                try:
                    async with httpx.AsyncClient(timeout=10) as cl:
                        r = await cl.post(
                            f"{self.base_url}/sendMessage",
                            json={"chat_id": chat_id, "text": text},
                        )
                        if r.status_code != 200:
                            sys.stderr.write(f"[TG ERROR] {chat_id}: {r.status_code} {r.text[:100]}\n")
                            return {"ok": False, "error": r.text[:100]}
                        return {"ok": True}
                except Exception as e:
                    sys.stderr.write(f"[TG EXCEPTION] {chat_id}: {e}\n")
                    return {"ok": False, "error": str(e)}
            return asyncio.run(_send())

    async def _do_send_async(self, text: str, chat_id: str, **kwargs) -> dict:
        """Async sendMessage via httpx."""
        if not self._token or not chat_id:
            return {"ok": False, "error": "Telegram not configured or no chat_id"}
        try:
            payload = {"chat_id": chat_id, "text": text}
            payload.update(kwargs)
            async with httpx.AsyncClient(timeout=10) as client:
                r = await client.post(
                    f"{self.base_url}/sendMessage",
                    json=payload,
                )
                data = r.json()
                if data.get("ok"):
                    msg_id = data.get("result", {}).get("message_id")
                    return {"ok": True, "message_id": msg_id}
                return {"ok": False, "error": data.get("description", "unknown")}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ════════════════════════════════════════════════════════════
    # 2FA Methods (static — no instance needed)
    # ════════════════════════════════════════════════════════════

    @staticmethod
    def send_2fa_code(user_id: int, chat_id: str, bot_token: str = None) -> str | None:
        """Generate & send a 6-digit 2FA code."""
        _token = bot_token or settings.telegram_bot_token
        if not _token or not chat_id:
            return None

        code = f"{secrets.randbelow(1000000):06d}"
        expires_at = time.time() + 300
        text = f"🔐 登录验证码\n\n{code}\n\n5分钟内有效，请勿泄露"

        try:
            payload = {"chat_id": chat_id, "text": text}
            r = requests.post(
                f"https://api.telegram.org/bot{_token}/sendMessage",
                json=payload,
                timeout=10,
            )
            data = r.json()
            if not data.get("ok"):
                return None

            _2fa_codes[user_id] = {
                "code": code,
                "expires_at": expires_at,
                "chat_id": chat_id,
                "message_id": data.get("result", {}).get("message_id"),
                "created_at": time.time(),
            }
            return code
        except Exception:
            return None

    @staticmethod
    def verify_2fa_code(user_id: int, code: str) -> bool:
        entry = _2fa_codes.pop(user_id, None)
        if not entry:
            return False
        if time.time() > entry["expires_at"]:
            return False
        return entry["code"] == code

    @staticmethod
    def get_2fa_entry(user_id: int) -> dict | None:
        return _2fa_codes.get(user_id)

    @staticmethod
    def delete_2fa_message(user_id: int) -> None:
        entry = _2fa_codes.get(user_id)
        if not entry:
            return
        _chat_id = entry.get("chat_id")
        msg_id = entry.get("message_id")
        if _chat_id and msg_id:
            TelegramService._delete_message_static(str(_chat_id), msg_id)

    @staticmethod
    def delete_message(chat_id: str, msg_id: int, bot_token: str = None) -> bool:
        """Delete a Telegram message by chat_id and message_id."""
        token = bot_token or settings.telegram_bot_token
        return TelegramService._delete_message_static(chat_id, msg_id, token)

    @staticmethod
    def _delete_message_static(chat_id: str, msg_id: int, token: str | None = None) -> bool:
        t = token or settings.telegram_bot_token
        if not t or not chat_id or not msg_id:
            return False
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{t}/deleteMessage",
                json={"chat_id": chat_id, "message_id": msg_id},
                timeout=5,
            )
            return r.json().get("ok", False)
        except Exception:
            return False
