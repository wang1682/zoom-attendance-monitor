"""
LongOnlineAlertService — 连续在线超时预警

负责：
- 检查 current_member_sessions 中连续在线超过 3 小时的成员
- 按租户推送预警（只推超过阈值的人）
- 去重：同一租户+会议+成员+alert_type 3 小时内不重复推送
- stale session 清理：is_online=1 且 last_activity_at 超过 6 小时无更新 → 标记离线
"""

import sys
from datetime import datetime, timezone, timedelta

import db as _db


class LongOnlineAlertService:
    """连续在线超时预警服务"""

    EVENT_TYPE = "online_timeout_alert"
    THRESHOLD_HOURS = 3
    STALE_THRESHOLD_HOURS = 6
    DEDUP_HOURS = 3  # 同一个 key 的去重窗口

    # ------------------------------------------------------------------
    # stale session 清理
    # ------------------------------------------------------------------

    @staticmethod
    def _clean_stale_sessions(tenant_id: str):
        """将超过 6 小时无更新的在线 session 标记为离线"""
        _cutoff = (datetime.now(timezone.utc) - timedelta(hours=LongOnlineAlertService.STALE_THRESHOLD_HOURS)).isoformat()
        conn = _db._get_conn()
        stale = conn.execute(
            """SELECT id, member_key, display_name, meeting_id, last_activity_at
               FROM current_member_sessions
               WHERE tenant_id=? AND is_online=1
                 AND last_activity_at < ?""",
            (tenant_id, _cutoff),
        ).fetchall()
        if not stale:
            return

        now_iso = datetime.now(timezone.utc).isoformat()
        for s in stale:
            conn.execute(
                """UPDATE current_member_sessions
                   SET is_online=0,
                       last_leave_at=?,
                       open_session_started_at=NULL,
                       updated_at=?
                   WHERE id=?""",
                (s["last_activity_at"], now_iso, s["id"]),
            )
            sys.stdout.write(
                f"[LONG_ONLINE] stale session cleaned: tenant={tenant_id} "
                f"member={s['display_name']} meeting={s['meeting_id']} "
                f"last_activity={s['last_activity_at']}\n"
            )
        conn.commit()

    # ------------------------------------------------------------------
    # 超时检查
    # ------------------------------------------------------------------

    @staticmethod
    def _find_timeout_members(tenant_id: str) -> list[dict]:
        """返回连续在线超过 3 小时的成员列表"""
        conn = _db._get_conn()
        now_utc = datetime.now(timezone.utc)
        cutoff = (now_utc - timedelta(hours=LongOnlineAlertService.THRESHOLD_HOURS)).isoformat()
        activity_cutoff = (now_utc - timedelta(minutes=10)).isoformat()

        rows = conn.execute(
            """SELECT member_key, display_name, meeting_id,
                      open_session_started_at, last_activity_at
               FROM current_member_sessions
               WHERE tenant_id=?
                 AND is_online=1
                 AND open_session_started_at IS NOT NULL
                 AND open_session_started_at <= ?
                 AND last_activity_at >= ?
               ORDER BY open_session_started_at ASC""",
            (tenant_id, cutoff, activity_cutoff),
        ).fetchall()

        results = []
        now_utc_dt = now_utc
        for r in rows:
            mins = 0
            try:
                jt = datetime.fromisoformat(r["open_session_started_at"].replace("Z", "+00:00"))
                mins = int((now_utc_dt - jt).total_seconds() / 60)
                h, m = mins // 60, mins % 60
                dur = f"{h}小时{m}分"
            except Exception:
                dur = "?"
            results.append({
                "member_key": r["member_key"],
                "display_name": r["display_name"],
                "meeting_id": r["meeting_id"],
                "duration": dur,
                "duration_minutes": mins,
                "open_session_started_at": r["open_session_started_at"],
                "last_activity_at": r["last_activity_at"],
            })
        return results

    # ------------------------------------------------------------------
    # 去重检查
    # ------------------------------------------------------------------

    @staticmethod
    def _can_alert(tenant_id: str, meeting_id: str, member_key: str, alert_type: str) -> bool:
        """检查是否在去重窗口内已经发送过"""
        conn = _db._get_conn()
        dedup_cutoff = (datetime.now(timezone.utc) - timedelta(hours=LongOnlineAlertService.DEDUP_HOURS)).isoformat()
        key = f"long_online:{tenant_id}:{meeting_id}:{member_key}:{alert_type}"
        existing = conn.execute(
            "SELECT id FROM alert_sent WHERE alert_key=? AND sent_at > ?",
            (key, dedup_cutoff),
        ).fetchone()
        return existing is None

    @staticmethod
    def _mark_alert_sent(tenant_id: str, meeting_id: str, member_key: str, alert_type: str):
        """记录已推送"""
        conn = _db._get_conn()
        key = f"long_online:{tenant_id}:{meeting_id}:{member_key}:{alert_type}"
        conn.execute(
            "INSERT OR IGNORE INTO alert_sent (alert_key, rule_type, sent_at) VALUES (?, ?, ?)",
            (key, LongOnlineAlertService.EVENT_TYPE, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    # ------------------------------------------------------------------
    # 按分组组织，生成推送文本
    # ------------------------------------------------------------------

    @staticmethod
    def _build_alert_text(members: list[dict]) -> str:
        """生成预警文本"""
        if not members:
            return ""

        # 分组
        from services.member import MemberService
        ms = MemberService()
        grouped = {}
        for m in members:
            grp = ms.resolve_display(m["display_name"], "default").get("group_name") or "其他"
            grouped.setdefault(grp, []).append(m)

        # 组排序：超时人数多的在前
        ordered = sorted(grouped.items(), key=lambda x: len(x[1]), reverse=True)

        lines = [
            "⏰ 连续在线超时预警",
            f"👥 超时人数：{len(members)}",
            f"阈值：{LongOnlineAlertService.THRESHOLD_HOURS}小时",
            "",
        ]
        for g, mems in ordered:
            lines.append(f"{g}（{len(mems)}）")
            for i, m in enumerate(mems, 1):
                lines.append(f"{i}. {m['display_name']} · {m['duration']}")
            lines.append("")

        return "\n".join(lines).strip()

    # ------------------------------------------------------------------
    # 主入口：检查并推送
    # ------------------------------------------------------------------

    @staticmethod
    async def check(tenant_id: str):
        """一次检查：清理 stale + 找超时成员 + 去重 + 推送"""
        # 1. 先清理 stale sessions
        LongOnlineAlertService._clean_stale_sessions(tenant_id)

        # 2. 找超时成员
        timeout_members = LongOnlineAlertService._find_timeout_members(tenant_id)
        if not timeout_members:
            return {"ok": False, "reason": "no_timeout_members", "count": 0}

        # 3. 去重：过滤掉已提醒过的
        to_alert = []
        for m in timeout_members:
            alert_type = f"long_online:{m['meeting_id']}:{m['member_key']}"
            if LongOnlineAlertService._can_alert(tenant_id, m["meeting_id"], m["member_key"], alert_type):
                to_alert.append(m)

        if not to_alert:
            return {"ok": False, "reason": "all_deduped", "count": len(timeout_members)}

        # 4. 生成文本
        text = LongOnlineAlertService._build_alert_text(to_alert)
        if not text:
            return {"ok": False, "reason": "no_text", "count": 0}

        # 5. 查租户规则并推送
        conn = _db._get_conn()
        rules = conn.execute(
            "SELECT id, target_channel_id FROM telegram_alert_rules "
            "WHERE event_type=? AND enabled=1 AND tenant_id=?",
            (LongOnlineAlertService.EVENT_TYPE, tenant_id),
        ).fetchall()

        if not rules:
            return {"ok": False, "reason": "no_rule", "count": len(to_alert)}

        sent_any = False
        for rule in rules:
            ch_id = rule["target_channel_id"]
            if not ch_id:
                continue
            ch_row = conn.execute(
                "SELECT chat_id, bot_token FROM telegram_channels WHERE id=? AND enabled=1",
                (ch_id,),
            ).fetchone()
            if not ch_row or not ch_row["bot_token"]:
                continue
            from services.telegram import TelegramService
            tg = TelegramService(token=ch_row["bot_token"], chat_id=ch_row["chat_id"])
            try:
                await tg.send_async(text)
                sent_any = True
            except Exception as e:
                sys.stderr.write(f"[LONG_ONLINE] 推送失败 rule_id={rule['id']} tenant={tenant_id} err={e}\n")

        # 6. 记录已推送（去重）
        if sent_any:
            for m in to_alert:
                alert_type = f"long_online:{m['meeting_id']}:{m['member_key']}"
                LongOnlineAlertService._mark_alert_sent(tenant_id, m["meeting_id"], m["member_key"], alert_type)
            sys.stdout.write(
                f"[LONG_ONLINE] 推送完成 tenant={tenant_id} "
                f"count={len(to_alert)}/{len(timeout_members)} (超时/已去重)\n"
            )

        return {"ok": sent_any, "count": len(to_alert), "total_timeout": len(timeout_members)}
