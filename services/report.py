"""
ReportService — 三小时在线报告生成与推送

重构后：直接复用成员中心 source of truth（db.get_today_attendance_summary）
不再自己算用户名和时长。
"""

import sys
from datetime import datetime, timezone, timedelta

import db as _db


class ReportService:
    """在线报告服务"""

    EVENT_TYPE = "periodic_online_report"

    def __init__(self):
        pass

    # ------------------------------------------------------------------
    # 时间检查
    # ------------------------------------------------------------------

    @staticmethod
    def get_report_hours() -> list[int]:
        """返回今天已经生成过报告的整点小时列表（UTC+8）"""
        conn = _db._get_conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        rows = conn.execute(
            "SELECT DISTINCT sent_at FROM alert_sent WHERE alert_key LIKE ?",
            (f"report:{today}:%",),
        ).fetchall()
        hours = []
        for r in rows:
            try:
                dt = datetime.fromisoformat(r[0])
                hours.append(dt.hour)
            except (ValueError, TypeError):
                continue
        return hours

    @staticmethod
    def _mark_report_sent(tenant_id: str, hour: int):
        """记录报告已发送"""
        conn = _db._get_conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        key = f"report:{today}:{hour}:{tenant_id}"
        conn.execute(
            "INSERT OR IGNORE INTO alert_sent (alert_key, rule_type, sent_at) VALUES (?, ?, ?)",
            (key, "periodic_online_report", datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()

    def should_report_now(self, tenant_id: str) -> bool:
        """判断当前是否应该生成报告（每 3 小时一次，租户隔离）"""
        myt_now = datetime.now(timezone.utc) + timedelta(hours=8)
        hour = myt_now.hour

        # 报告时段: 0,3,6,9,12,15,18,21
        if hour % 3 != 0:
            return False

        sent_hours = self.get_report_hours()
        return hour not in sent_hours

    # ------------------------------------------------------------------
    # 报告生成 — 复用成员中心 source of truth
    # ------------------------------------------------------------------

    def build_report(self, tenant_id: str = "default") -> dict:
        """生成在线报告内容

        Returns:
            {"text": "...", "participant_count": N}
        """
        # 直接从成员中心口径获取，与 dashboard/participants 一致
        summary = _db.get_today_attendance_summary(tenant_id)
        members = summary.get("members", [])

        # 只取在线成员
        online = [m for m in members if m.get("is_online")]

        # 按分组聚合
        grouped = {}
        for m in online:
            sn = m.get("standard_name", "") or m.get("name", "")
            grp = m.get("group_name") or "未分组"
            secs = m.get("today_total_seconds", 0)
            grouped.setdefault(grp, []).append((sn, secs))

        for g in grouped:
            grouped[g].sort(key=lambda x: x[1], reverse=True)

        ordered = []
        priority_groups = ("核销", "推进")
        for pg in priority_groups:
            if pg in grouped:
                ordered.append((pg, grouped.pop(pg)))
        for g, members in grouped.items():
            ordered.append((g, members))

        lines = ["🟠 实时在线", f"👥 在线人数：{len(online)}", ""]
        if online:
            g_emoji = {"核销": "🔵", "推进": "🟡"}
            for g, members in ordered:
                emoji = g_emoji.get(g, "⚪")
                lines.append(f"{emoji} {g}（{len(members)}）")
                for i, (sn, secs) in enumerate(members, 1):
                    h, m = secs // 3600, (secs % 3600) // 60
                    dur = f"{h}小时{m}分" if h > 0 else f"{m}分钟"
                    lines.append(f"{i}. {sn} · {dur}")
                lines.append("")
        else:
            lines.append("当前无人在线")
            lines.append("")

        return {"text": "\n".join(lines), "participant_count": len(online)}

    # ------------------------------------------------------------------
    # 报告推送
    # ------------------------------------------------------------------

    async def send_report(self, tenant_id: str = "default") -> dict:
        """生成并推送在线报告（如果满足时间条件）"""
        myt_now = datetime.now(timezone.utc) + timedelta(hours=8)
        hour = myt_now.hour

        if not self.should_report_now(tenant_id):
            return {"ok": False, "reason": "not_report_time"}

        report = self.build_report(tenant_id)

        # 查这个租户的 periodic_online_report 规则
        conn = _db._get_conn()
        rules = conn.execute(
            "SELECT id, target_channel_id, tenant_id "
            "FROM telegram_alert_rules "
            "WHERE event_type=? AND enabled=1 AND tenant_id=?",
            (self.EVENT_TYPE, tenant_id),
        ).fetchall()

        sent_any = False
        for rule in rules:
            ch_id = rule["target_channel_id"]
            if not ch_id:
                continue
            ch_row = conn.execute(
                "SELECT chat_id, bot_token, bot_username FROM telegram_channels WHERE id=? AND enabled=1",
                (ch_id,),
            ).fetchone()
            if not ch_row or not ch_row["bot_token"]:
                continue
            from services.telegram import TelegramService

            tg = TelegramService(token=ch_row["bot_token"], chat_id=ch_row["chat_id"])
            try:
                await tg.send_async(report["text"])
                sys.stdout.write(
                    f"[PERIODIC REPORT] 推送至 rule_id={rule['id']} "
                    f"tenant={tenant_id} ({report['participant_count']}人)\n"
                )
                sent_any = True
            except Exception as e:
                sys.stderr.write(
                    f"[PERIODIC REPORT] 推送失败 rule_id={rule['id']} "
                    f"tenant={tenant_id} err={e}\n"
                )

        if sent_any:
            self._mark_report_sent(tenant_id, hour)

        return {"ok": sent_any, "participant_count": report["participant_count"]}
