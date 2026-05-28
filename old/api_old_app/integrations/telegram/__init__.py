"""
telegram.py — Telegram 推送服务

每次发送自动写入 alert_logs（Phase 2）
"""
from __future__ import annotations

import sys
import httpx
from app.settings import settings
from phase2.event_writer import write_alert_log


class TelegramNotifier:
    """Telegram 消息推送"""

    def __init__(self, token: str = ""):
        self.token = token or settings.telegram_bot_token
        self._base = f"https://api.telegram.org/bot{self.token}"

    def _classify(self, text: str) -> str:
        """根据消息内容判断类型"""
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
        """发送消息并记录 alert_logs"""
        targets = [chat_id] if chat_id else [settings.telegram_private_chat_id]

        if group and settings.telegram_group_enabled and settings.telegram_group_chat_id:
            targets.append(settings.telegram_group_chat_id)

        success = True
        async with httpx.AsyncClient(timeout=10) as client:
            for cid in targets:
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

        # 写 alert_logs
        mtype = self._classify(text)
        title = text.split("\n")[0] if text else "推送"
        try:
            write_alert_log(
                message_type=mtype,
                title=title.strip(" *"),
                content=text[:200],
                recipient=",".join(targets),
                success=success,
                error_message="" if success else "发送失败",
            )
        except Exception as e:
            sys.stderr.write(f"[ALERT LOG ERROR] {e}\n")

        return success

    async def health(self) -> bool:
        """检查 bot token 是否有效"""
        if not self.token:
            return False
        async with httpx.AsyncClient(timeout=5) as client:
            try:
                r = await client.get(f"{self._base}/getMe")
                return r.status_code == 200 and r.json().get("ok")
            except Exception:
                return False
