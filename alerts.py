"""
alerts.py — Telegram 消息推送
"""
from __future__ import annotations

import sys
import httpx
from config import settings
from db import create_alert as _save_alert, log_alert_sent


class TelegramNotifier:
    def __init__(self, token: str = ""):
        self.token = token or settings.telegram_bot_token
        self._base = f"https://api.telegram.org/bot{self.token}"

    def _classify(self, text: str) -> str:
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

    async def send(self, text: str, chat_id: str = "", group: bool = False) -> bool:
        # ── push_enabled / quiet_mode 检查 ──
        from db import is_push_enabled, is_quiet_mode
        if not is_push_enabled():
            sys.stdout.write("[ALERTS] push_enabled=off, 跳过推送\n")
            sys.stdout.flush()
            return False
        if is_quiet_mode():
            mtype = self._classify(text)
            # 静默模式下仅放行关键事件
            critical_types = {"stranger"}
            if mtype not in critical_types:
                sys.stdout.write(f"[ALERTS] quiet_mode=on, 跳过非关键推送 ({mtype})\n")
                sys.stdout.flush()
                return False
        targets = [chat_id] if chat_id else [settings.telegram_private_chat_id]
        if group and settings.telegram_group_enabled and settings.telegram_group_chat_id:
            targets.append(settings.telegram_group_chat_id)

        # 写 alert 日志
        mtype = self._classify(text)
        title = text.split("\n")[0] if text else "推送"
        alert_id = _save_alert(
            alert_type=mtype,
            title=title.strip(" *"),
            message=text[:500],
        )

        success = True
        async with httpx.AsyncClient(timeout=10) as client:
            for cid in targets:
                if not cid:
                    continue
                try:
                    r = await client.post(
                        f"{self._base}/sendMessage",
                        json={"chat_id": cid, "text": text, "parse_mode": "Markdown"},
                    )
                    if r.status_code != 200:
                        success = False
                        sys.stderr.write(f"[TG ERROR] {cid}: {r.status_code} {r.text[:100]}\n")
                except Exception as e:
                    success = False
                    sys.stderr.write(f"[TG EXCEPTION] {cid}: {e}\n")

        log_alert_sent(alert_id, ",".join(filter(None, targets)), success)
        return success

    async def health(self) -> bool:
        if not self.token:
            return False
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                r = await client.get(f"{self._base}/getMe")
                return r.status_code == 200 and r.json().get("ok")
            except Exception:
                return False
