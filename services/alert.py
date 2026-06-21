"""
AlertService — 统一预警规则管理与 Webhook 事件触发推送

负责：
- 规则 CRUD（按租户隔离）
- 规则测试
- should_push（cooldown + quiet hours + 租户校验）
- webhook 事件触发 → 规则匹配 → Telegram 推送
"""

import sys
import json
import re
import traceback
from datetime import datetime, timezone, timedelta, time as dt_time

import db as _db

# ---------------------------------------------------------------------------
# 内部: 禁用推送的 event_type 硬名单
# ---------------------------------------------------------------------------
_HARD_BLOCKED = frozenset({
    "participant_joined_breakout_room",
    "participant_left_breakout_room",
    "breakout_room_joined",
    "breakout_room_left",
})


class AlertService:
    """预警规则服务（支持多租户）"""

    # ------------------------------------------------------------------
    # 规则 CRUD
    # ------------------------------------------------------------------

    def get_rules(self, tenant_id: str) -> list[dict]:
        """获取租户规则列表，附带 channels"""
        rules = _db.get_telegram_rules_by_tenant(tenant_id)
        for r in rules:
            r["channel_ids"] = _db.get_alert_rule_channels(r["event_type"])
        return rules

    def get_rule(self, tenant_id: str, event_type: str) -> dict | None:
        """获取单个规则"""
        rule = _db.get_telegram_rule_by_event(event_type)
        if rule and rule.get("tenant_id") != tenant_id:
            return None
        return rule

    def upsert_rule(self, tenant_id: str, event_type: str, data: dict) -> int:
        """创建或更新规则"""
        rule_id = _db.upsert_telegram_rule(tenant_id, event_type, data)
        # 如果 data 中有 channel_ids，更新关联
        if "channel_ids" in data:
            _db.set_alert_rule_channels(event_type, data["channel_ids"])
        return rule_id

    def delete_rule(self, tenant_id: str, event_type: str) -> bool:
        """删除规则"""
        result = _db.delete_telegram_rule(tenant_id, event_type)
        if result:
            _db.set_alert_rule_channels(event_type, [])
        return result

    def update_rule_channels(self, event_type: str, channel_ids: list[int]):
        """更新规则关联的 channel_ids"""
        _db.set_alert_rule_channels(event_type, channel_ids)

    # ------------------------------------------------------------------
    # should_push — 判断是否应该发送此事件的推送
    # ------------------------------------------------------------------

    def should_push(self, event_type: str, tenant_id: str = None) -> bool:
        """判断是否允许推送此事件（tenant-aware should_send_telegram）"""
        # 0. 硬拦截
        if event_type in _HARD_BLOCKED:
            return False

        conn = _db._get_conn()
        if tenant_id:
            rule = conn.execute(
                "SELECT * FROM telegram_alert_rules WHERE event_type=? AND tenant_id=?",
                (event_type, tenant_id),
            ).fetchone()
        else:
            rule = conn.execute(
                "SELECT * FROM telegram_alert_rules WHERE event_type=?",
                (event_type,),
            ).fetchone()

        if not rule:
            return True  # 无规则 → 兼容旧行为

        rule = dict(rule)

        # 未启用
        if not rule.get("enabled", 0):
            return False

        # Cooldown
        alert_key = f"telegram:{event_type}"
        last = conn.execute(
            "SELECT sent_at FROM alert_sent WHERE alert_key=? ORDER BY id DESC LIMIT 1",
            (alert_key,),
        ).fetchone()
        if last and last["sent_at"]:
            try:
                elapsed = (datetime.now(timezone.utc) - datetime.fromisoformat(last["sent_at"])).total_seconds()
                cd = rule.get("cooldown_seconds", 0)
                if cd > 0 and elapsed < cd:
                    return False
            except (ValueError, TypeError):
                pass

        # Quiet hours
        if rule.get("quiet_enabled", 0):
            if self._within_quiet_hours(rule):
                return False

        return True

    @staticmethod
    def _within_quiet_hours(rule: dict) -> bool:
        """MYT 时间是否在静默时段内"""
        myt_now = datetime.now(timezone.utc) + timedelta(hours=8)
        current = myt_now.time()
        try:
            sp = rule["quiet_start"].split(":")
            ep = rule["quiet_end"].split(":")
            start = dt_time(int(sp[0]), int(sp[1]))
            end = dt_time(int(ep[0]), int(ep[1]))
        except (ValueError, IndexError, KeyError):
            return False
        if start <= end:
            return start <= current <= end
        else:
            return current >= start or current <= end

    # ------------------------------------------------------------------
    # 规则测试 — 直接发测试消息到规则的 target_channel
    # ------------------------------------------------------------------

    def test_rule(self, tenant_id: str, event_type: str) -> dict:
        """测试规则推送消息"""
        rules = self.get_rules(tenant_id)
        rule = next((r for r in rules if r["event_type"] == event_type), None)
        if not rule:
            return {"ok": False, "error": "规则不存在"}

        target_id = rule.get("target_channel_id")
        if not target_id:
            return {"ok": False, "error": "推送目标未配置"}

        channel = _db.get_telegram_channel_by_id(target_id) if target_id else None
        if not channel or not channel.get("bot_token") or not channel.get("chat_id"):
            return {"ok": False, "error": "推送目标未配置"}

        title = rule.get("title") or event_type
        text = (
            "✅ 测试规则推送\n\n"
            f"规则: {title}\n"
            f"事件: {event_type}\n"
            "状态: 推送配置正常"
        )

        from services.telegram import TelegramService
        tg = TelegramService(token=channel["bot_token"], chat_id=channel["chat_id"])
        result = tg.send(text)
        return result

    # ------------------------------------------------------------------
    # webhook 事件触发 → 推送
    # ------------------------------------------------------------------

    def handle_webhook_event(
        self,
        payload: dict,
        event_type: str,
        tenant_id: str,
        *,
        account_id: str = "",
        bot_token_override: str = "",
    ) -> None:
        """webhook 事件触发 → 按规则判断 → Telegram 推送 + alert_sent + alerts 日志

        这是 app.py 中 webhook 路由 (L1491-1670) 的内聚版本。
        """
        now_utc = datetime.now(timezone.utc)
        MYT = timezone(timedelta(hours=8))
        now_myt_str = now_utc.astimezone(MYT).strftime("%m-%d %H:%M:%S")

        obj = payload.get("payload", {}).get("object", payload.get("object", {}))
        participant = obj.get("participant", {})
        pid = str(participant.get("user_id", "")) or str(participant.get("id", ""))
        ename = participant.get("user_name", "").strip()
        sd = participant.get("sharing_details", {})
        sdt = sd.get("date_time", "")

        # ── 解析事件类型与推送文案 ──
        push_event, push_icon, push_title = self._classify_webhook_event(event_type)

        if not push_title or not ename:
            return

        mid = str(obj.get("id", ""))
        event_ts = sdt or now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")

        # resolve member（统一走 MemberService，tenant-aware）
        from services.member import MemberService
        ms = MemberService()
        rm = ms.resolve_display(ename, tenant_id)
        standard_name = rm.get("display_name", ename)
        group_name = rm.get("group_name") or ""
        is_mapped = rm.get("is_configured", False)
        sys.stderr.write(f"[PUSH] member_resolve tenant={tenant_id} raw={ename} configured={is_mapped} group={group_name}\n")

        user_key = pid or standard_name.strip().lower().replace(" ", "")
        dedup_key = f"webhook:{push_event}:{mid}:{user_key}:{event_ts[:16]}"
        sys.stderr.write(f"[PUSH] dedup_key={dedup_key}\n")

        # ── 规则门控 ──
        if not self.should_push(push_event, tenant_id):
            sys.stderr.write(f"[PUSH] {push_event} blocked by rule (should_push=False)\n")
            sys.stderr.flush()
            return

        # ── sharing_ended: 检查是否存在 active sharing ──
        if push_event == "sharing_ended":
            conn_chk = _db._get_conn()
            active = conn_chk.execute(
                "SELECT id FROM sharing_live"
                " WHERE tenant_id=? AND meeting_id=? AND user_name=? AND is_active=1"
                " LIMIT 1",
                (tenant_id, mid, ename),
            ).fetchone()
            if not active:
                sys.stderr.write(f"[PUSH] sharing_ended ignored (no active sharing) tenant={tenant_id} mid={mid} user={ename}\n")
                sys.stderr.flush()
                return

        conn = _db._get_conn()
        already = conn.execute(
            "SELECT 1 FROM alert_sent WHERE alert_key=?", (dedup_key,)
        ).fetchone()
        if already:
            sys.stderr.write("[PUSH] duplicate, skipped\n")
            sys.stderr.flush()
            return

        # ── 构建消息 ──
        content_type = sd.get("content", "") if push_event in ("sharing_started", "sharing_ended") else ""
        extra_line = f"\n📄 内容: {content_type}" if content_type else ""

        # 自定义标题(子类型)
        if group_name:
            if push_event == "participant_joined":
                title_line = f"📌 {standard_name} 进入【{group_name}】主会议"
            elif push_event == "participant_left":
                title_line = f"🚪 {standard_name} 离开【{group_name}】"
            elif push_event == "waiting_room_joined":
                title_line = f"⏳ {standard_name} 在等候室"
            elif push_event == "sharing_started":
                title_line = f"🖥 {standard_name} 开始共享屏幕"
            elif push_event == "sharing_ended":
                title_line = f"🖥 {standard_name} 结束共享屏幕"
            else:
                title_line = push_title
        elif is_mapped:
            title_line = push_title
        else:
            title_line = f"未配置成员 {standard_name} {push_title}"

        text = (
            f"{push_icon} *{title_line}*\n\n"
            f"👆 {standard_name}\n"
            f"🔔 会议: {mid}\n"
            f"⏰ {now_myt_str}"
            f"{extra_line}"
        )

        # ── 解析推送目标 ──
        targets = []
        try:
            channels = _db.get_tenant_channels(tenant_id) if tenant_id else []
            for ch in channels:
                if not ch.get("is_enabled", 1):
                    continue
                c_bot = ch.get("bot_token", "") or bot_token_override or None
                if c_bot and ch.get("chat_id"):
                    targets.append({"chat_id": ch["chat_id"], "bot_token": c_bot})
        except Exception:
            pass

        if not targets:
            sys.stderr.write(f"[PUSH] no enabled tenant_channels for {tenant_id}, skipping\n")
            sys.stderr.flush()
            return

        from telegram_push import send_message

        result = {"ok": False, "error": "no targets"}
        for t in targets:
            result = send_message(text, chat_id=t["chat_id"], bot_token=t["bot_token"] or None)
            sys.stderr.write(f"[PUSH] send to {t['chat_id']}: {result}\n")
            sys.stderr.flush()

        # ── 记录推送日志 ──
        if result.get("ok"):
            safe_text = lambda v: str(v).encode("utf-8", "ignore").decode("utf-8", "ignore") if v else ""
            conn.execute(
                "INSERT OR REPLACE INTO alert_sent (alert_key, rule_type, sent_at) VALUES (?, ?, ?)",
                (safe_text(dedup_key), "webhook_event_push", now_utc.isoformat()),
            )
            conn.execute(
                """INSERT INTO alerts (
                    alert_type, severity, title, message, related_name,
                    success, created_at, tenant_id, event_type,
                    target_channel_id, telegram_chat_id, status, error_message
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    "webhook_push",
                    "info",
                    safe_text(push_title),
                    safe_text(text),
                    safe_text(standard_name),
                    1,
                    now_utc.isoformat(),
                    tenant_id or "default",
                    safe_text(push_event),
                    0,
                    "",
                    "sent",
                    "",
                ),
            )
            conn.commit()
            sys.stderr.write("[PUSH] inserted alert_sent + alerts\n")
        else:
            sys.stderr.write(f"[PUSH] send failed: {result.get('error', '')}\n")

        sys.stderr.flush()

    # ------------------------------------------------------------------
    # 内部：webhook 事件分类
    # ------------------------------------------------------------------

    @staticmethod
    def _classify_webhook_event(raw: str) -> tuple[str, str, str]:
        """将 Zoom webhook event_type 分类为 (push_event, icon, title)"""
        if "breakout_room" in raw:
            if "participant_joined" in raw:
                return ("participant_joined_breakout_room", "📌", "加入分组讨论室")
            elif "participant_left" in raw:
                return ("participant_left_breakout_room", "🚪", "离开分组讨论室")
            elif "sharing_started" in raw:
                return ("sharing_started", "🖥", "分组讨论室开始共享屏幕")
            elif "sharing_ended" in raw:
                return ("sharing_ended", "🖥", "分组讨论室结束共享屏幕")
        if "participant_joined" in raw and "waiting_room" not in raw:
            return ("participant_joined", "📌", "进入主会议")
        if "participant_left" in raw:
            return ("participant_left", "🚪", "离开会议")
        if "waiting_room" in raw and "joined" in raw:
            return ("waiting_room_joined", "⏳", "在等候室")
        if "admitted" in raw:
            return ("admitted", "✅", "等候室成员已准入")
        if "sharing_started" in raw:
            return ("sharing_started", "🖥", "开始共享屏幕")
        if "sharing_ended" in raw:
            return ("sharing_ended", "🖥", "结束共享屏幕")
        return ("unknown", "ℹ️", "")
