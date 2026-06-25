"""
ReportService — 三小时在线报告生成与推送

重构后：直接复用成员中心 source of truth（db.get_today_attendance_summary）
在线名单口径与 /dashboard/participants 完全一致（Zoom Metrics API）。
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

        在线名单口径 = Zoom Metrics API 实时参与者（与面板一致）。
        累计时长 = get_today_attendance_summary（成员中心 source of truth）。

        Returns:
            {"text": "...", "participant_count": N}
        """
        # 1. 从 Zoom Metrics API 获取当前在线成员（与面板同一口径）
        online_names = _db.get_live_online_standard_names(tenant_id)

        if not online_names:
            return {"text": "🟠 实时在线\n👥 在线人数：0\n\n当前无人在线", "participant_count": 0}

        # 2. 从成员中心获取今日累计数据
        summary = _db.get_today_attendance_summary(tenant_id)
        members = summary.get("members", [])

        # 3. 只取 Metrics 判定在线的人，用成员中心的数据补全累计
        online = []
        known = {m.get("standard_name", ""): m for m in members}
        for sn in sorted(online_names):
            m = known.get(sn, {})
            secs = m.get("today_total_seconds", 0)
            grp = m.get("group_name") or "未分组"
            online.append((sn, secs, grp))

        # 按分组聚合排序
        grouped = {}
        for sn, secs, grp in online:
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
        g_emoji = {"核销": "🔵", "推进": "🟡"}
        for g, members in ordered:
            emoji = g_emoji.get(g, "⚪")
            lines.append(f"{emoji} {g}（{len(members)}）")
            for i, (sn, secs) in enumerate(members, 1):
                h, m = secs // 3600, (secs % 3600) // 60
                dur = f"{h}小时{m}分" if h > 0 else f"{m}分钟"
                lines.append(f"{i}. {sn} · {dur}")
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
