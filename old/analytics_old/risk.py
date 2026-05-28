"""risk.py — 风险检测引擎
从 participant_daily_stats 检测风险，写入 risk_scores
同人同天同 risk_type 不重复推送（ON CONFLICT DO NOTHING 语义）
"""
from __future__ import annotations

import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from datetime import datetime, timezone, timedelta
from app.database import SyncSession
from app.settings import settings
from app.analytics.models import RiskScore

MYT = timezone(timedelta(hours=8))


def _today_str() -> str:
    return datetime.now(MYT).strftime("%Y-%m-%d")


def _check_existing(session, tenant_id: str, name: str, risk_type: str, date_str: str) -> bool:
    """检查是否已存在同人同天同类型风险（去重）"""
    return session.query(RiskScore).filter(
        RiskScore.tenant_id == tenant_id,
        RiskScore.name == name,
        RiskScore.risk_type == risk_type,
        RiskScore.date == date_str,
    ).first() is not None


def _upsert_risk(session, tenant_id: str, name: str, email: str,
                 risk_type: str, severity: str, description: str,
                 date_str: str) -> bool:
    """INSERT ON CONFLICT DO NOTHING 写入 risk_scores

    唯一键: (tenant_id, name, risk_type, date)
    返回 True 表示新插入，False 表示已存在跳过
    """
    from sqlalchemy import text
    result = session.execute(text("""
        INSERT OR IGNORE INTO risk_scores (tenant_id, name, email, risk_type, severity, description, date)
        VALUES (:t, :n, :e, :rt, :s, :desc, :d)
    """), {
        "t": tenant_id, "n": name, "e": email or "",
        "rt": risk_type, "s": severity,
        "desc": description, "d": date_str,
    })
    return result.rowcount > 0


def detect_short_stay(session: SyncSession, tenant_id: str, date_str: str = None) -> list[dict]:
    """短停留风险 — 停留 <3 分钟"""
    date_str = date_str or _today_str()
    results = []

    from app.analytics.models import ParticipantDailyStat

    records = session.query(ParticipantDailyStat).filter(
        ParticipantDailyStat.tenant_id == tenant_id,
        ParticipantDailyStat.date == date_str,
        ParticipantDailyStat.short_stay == True,  # noqa: E712
    ).all()

    for rec in records:
        if _upsert_risk(session, tenant_id, rec.name, rec.email,
                        "short_stay", "medium",
                        f"疑似挂机: 停留仅 {rec.total_duration_minutes:.1f} 分钟（<3分钟），进入 {rec.enter_count} 次",
                        date_str):
            results.append({"name": rec.name, "risk_type": "short_stay", "duration": rec.total_duration_minutes})

    session.commit()
    return results


def detect_late(session: SyncSession, tenant_id: str, date_str: str = None) -> list[dict]:
    """迟到风险 — 晚于 signin_deadline_hour"""
    date_str = date_str or _today_str()
    results = []

    from app.analytics.models import ParticipantDailyStat

    records = session.query(ParticipantDailyStat).filter(
        ParticipantDailyStat.tenant_id == tenant_id,
        ParticipantDailyStat.date == date_str,
        ParticipantDailyStat.late_entry == True,  # noqa: E712
    ).all()

    for rec in records:
        desc = f"迟到: {date_str} 首次入场时间 "
        if rec.first_entry:
            desc += rec.first_entry.strftime("%H:%M")
        else:
            desc += "无记录"

        severity = "low"
        if rec.consecutive_late_days >= 5:
            severity = "critical"
        elif rec.consecutive_late_days >= 3:
            severity = "high"

        if _upsert_risk(session, tenant_id, rec.name, rec.email,
                        "late", severity,
                        f"{desc}（连续迟到 {rec.consecutive_late_days} 天）",
                        date_str):
            results.append({"name": rec.name, "risk_type": "late", "consecutive": rec.consecutive_late_days})

        # 连续 3 天以上额外生成 consecutive_late
        if rec.consecutive_late_days >= 3:
            cl_type = "consecutive_late"
            if _upsert_risk(session, tenant_id, rec.name, rec.email,
                            cl_type,
                            "high" if rec.consecutive_late_days >= 5 else "medium",
                            f"连续迟到 {rec.consecutive_late_days} 天 — 需关注！",
                            date_str):
                results.append({"name": rec.name, "risk_type": cl_type, "consecutive": rec.consecutive_late_days})

    session.commit()
    return results


def detect_night_entry(session: SyncSession, tenant_id: str, date_str: str = None) -> list[dict]:
    """深夜进入风险 — 23:00-06:00"""
    date_str = date_str or _today_str()
    results = []

    from app.analytics.models import ParticipantDailyStat

    records = session.query(ParticipantDailyStat).filter(
        ParticipantDailyStat.tenant_id == tenant_id,
        ParticipantDailyStat.date == date_str,
        ParticipantDailyStat.night_entry == True,  # noqa: E712
    ).all()

    for rec in records:
        desc = f"深夜进入: "
        if rec.first_entry:
            desc += rec.first_entry.strftime("%H:%M")
        else:
            desc += "无记录"

        if _upsert_risk(session, tenant_id, rec.name, rec.email,
                        "night_entry",
                        "high" if rec.first_entry and rec.first_entry.hour < 4 else "medium",
                        desc,
                        date_str):
            results.append({"name": rec.name, "risk_type": "night_entry"})

    session.commit()
    return results


def detect_low_activity(session: SyncSession, tenant_id: str, date_str: str = None,
                        min_duration: float = 10.0) -> list[dict]:
    """低活跃风险 — 总停留时长 < min_duration 分钟"""
    date_str = date_str or _today_str()
    results = []

    from app.analytics.models import ParticipantDailyStat

    records = session.query(ParticipantDailyStat).filter(
        ParticipantDailyStat.tenant_id == tenant_id,
        ParticipantDailyStat.date == date_str,
        ParticipantDailyStat.total_duration_minutes > 0,
        ParticipantDailyStat.total_duration_minutes < min_duration,
    ).all()

    for rec in records:
        if rec.short_stay:
            continue  # 短停留已由 short_stay 覆盖

        if _upsert_risk(session, tenant_id, rec.name, rec.email,
                        "low_activity", "medium",
                        f"低活跃: 停留仅 {rec.total_duration_minutes:.1f} 分钟（<{min_duration}分钟）",
                        date_str):
            results.append({"name": rec.name, "risk_type": "low_activity", "duration": rec.total_duration_minutes})

    session.commit()
    return results


def detect_all(session: SyncSession = None, date_str: str = None,
               tenant_id: str | None = None) -> dict:
    """执行全部风险检测"""
    close_session = False
    if session is None:
        session = SyncSession()
        close_session = True
    try:
        date_str = date_str or _today_str()
        tenant = tenant_id or settings.default_tenant_id

        results = {
            "short_stay": detect_short_stay(session, tenant, date_str),
            "late": detect_late(session, tenant, date_str),
            "night_entry": detect_night_entry(session, tenant, date_str),
            "low_activity": detect_low_activity(session, tenant, date_str),
        }

        total = sum(len(v) for v in results.values())
        return {"total": total, "details": results}
    finally:
        if close_session:
            session.close()


if __name__ == "__main__":
    result = detect_all()
    sys.stdout.write(f"[RISK] 检测完成: 共 {result['total']} 条新风险\n")
    for rtype, items in result["details"].items():
        if items:
            sys.stdout.write(f"  {rtype}: {len(items)} 条\n")
