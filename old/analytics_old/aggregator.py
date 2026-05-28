"""aggregator.py — 聚合引擎
从 participant_events / zoom_participants 聚合到 daily_stats, person_stats, hourly_activity, participant_daily_stats

所有操作使用 INSERT ... ON CONFLICT DO UPDATE 幂等写入。
Phase 8: 所有聚合函数接受 tenant_id 参数，不再硬编码 default_tenant_id。
P8.6: 全部使用 raw SQL upsert，确保连续跑 N 次不报 UNIQUE constraint。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

# 确保可以 import app 包
_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from app.database import SyncSession
from app.settings import settings
from app.models import DailyStat, PersonStat, HourlyActivity
from app.analytics.models import ParticipantDailyStat

MYT = timezone(timedelta(hours=8))


def _today_str() -> str:
    return datetime.now(MYT).strftime("%Y-%m-%d")


def _date_range(days_back: int = 7) -> list[str]:
    today = datetime.now(MYT).date()
    return [(today - timedelta(days=i)).isoformat() for i in range(days_back)]


def _upsert_daily_stat(session: SyncSession, tenant_id: str, date_str: str,
                       total_persons: int, total_duration: float,
                       earliest_str: str, latest_str: str,
                       unique_emails: int) -> None:
    """INSERT ... ON CONFLICT DO UPDATE 写入 daily_stats"""
    from sqlalchemy import text
    session.execute(text("""
        INSERT INTO daily_stats (tenant_id, date, total_persons, total_duration_minutes, earliest_entry, latest_entry, unique_emails, updated_at)
        VALUES (:t, :d, :p, :dur, :e, :l, :ue, datetime('now'))
        ON CONFLICT(tenant_id, date) DO UPDATE SET
            total_persons = :p2,
            total_duration_minutes = :dur2,
            earliest_entry = :e2,
            latest_entry = :l2,
            unique_emails = :ue2,
            updated_at = datetime('now')
    """), {
        "t": tenant_id, "d": date_str,
        "p": total_persons, "dur": total_duration,
        "e": earliest_str, "l": latest_str, "ue": unique_emails,
        "p2": total_persons, "dur2": total_duration,
        "e2": earliest_str, "l2": latest_str, "ue2": unique_emails,
    })


def aggregate_daily_stats(
    session: SyncSession = None,
    date_str: str = None,
    tenant_id: str | None = None,
) -> dict:
    """聚合 daily_stats — 天级汇总，幂等 upsert"""
    close_session = False
    if session is None:
        session = SyncSession()
        close_session = True
    try:
        date_str = date_str or _today_str()
        tenant = tenant_id or settings.default_tenant_id

        from sqlalchemy import text

        rows = session.execute(text("""
            SELECT
                COUNT(DISTINCT name) as total_persons,
                COALESCE(SUM(CASE WHEN action='enter' THEN 1 ELSE 0 END), 0) as total_entries,
                COUNT(DISTINCT email) as unique_emails
            FROM participant_events
            WHERE tenant_id = :t AND date(action_time) = :d
        """), {"t": tenant, "d": date_str}).fetchone()

        total_persons = rows[0] or 0
        unique_emails = rows[2] or 0

        earliest = session.execute(text("""
            SELECT MIN(action_time) FROM participant_events
            WHERE tenant_id = :t AND date(action_time) = :d AND action = 'enter'
        """), {"t": tenant, "d": date_str}).scalar()
        latest = session.execute(text("""
            SELECT MAX(action_time) FROM participant_events
            WHERE tenant_id = :t AND date(action_time) = :d AND action = 'enter'
        """), {"t": tenant, "d": date_str}).scalar()

        dur_row = session.execute(text("""
            SELECT COALESCE(SUM(total_duration_minutes), 0)
            FROM person_stats
            WHERE tenant_id = :t AND date = :d
        """), {"t": tenant, "d": date_str}).scalar()
        total_duration = float(dur_row or 0.0)

        earliest_str = earliest.strftime("%H:%M") if isinstance(earliest, datetime) else str(earliest or "")
        latest_str = latest.strftime("%H:%M") if isinstance(latest, datetime) else str(latest or "")

        # 幂等 upsert
        _upsert_daily_stat(session, tenant, date_str,
                           total_persons, total_duration,
                           earliest_str, latest_str, unique_emails)

        session.commit()
        return {
            "date": date_str, "total_persons": total_persons,
            "total_duration_minutes": total_duration,
            "earliest_entry": earliest_str, "latest_entry": latest_str,
            "unique_emails": unique_emails,
        }
    finally:
        if close_session:
            session.close()


def _upsert_person_stat(session: SyncSession, tenant_id: str, date_str: str,
                        name: str, email: str, first_str: str, last_str: str,
                        total_min: float, enter_count: int) -> None:
    """INSERT ... ON CONFLICT DO UPDATE 写入 person_stats

    唯一键: (tenant_id, date, name)
    因 SQLite ON CONFLICT 需要 unique index 而非 auto-increment pk，
    使用 raw INSERT + 备选方案避免竞争。
    """
    from sqlalchemy import text

    # 先检查是否存在
    existing = session.execute(text("""
        SELECT id FROM person_stats
        WHERE tenant_id = :t AND date = :d AND name = :n
        LIMIT 1
    """), {"t": tenant_id, "d": date_str, "n": name}).scalar()

    if existing:
        session.execute(text("""
            UPDATE person_stats
            SET email = :email, first_entry = :fe, last_leave = :ll,
                total_duration_minutes = :dur, enter_count = :ec,
                updated_at = datetime('now')
            WHERE id = :id
        """), {
            "id": existing, "email": email, "fe": first_str,
            "ll": last_str, "dur": total_min, "ec": enter_count,
        })
    else:
        session.execute(text("""
            INSERT INTO person_stats (date, tenant_id, name, email, first_entry, last_leave, total_duration_minutes, enter_count, updated_at)
            VALUES (:d, :t, :n, :email, :fe, :ll, :dur, :ec, datetime('now'))
        """), {
            "d": date_str, "t": tenant_id, "n": name,
            "email": email, "fe": first_str, "ll": last_str,
            "dur": total_min, "ec": enter_count,
        })


def _upsert_participant_daily_stat(session: SyncSession, tenant_id: str, date_str: str,
                                   name: str, email: str,
                                   fe_dt, ll_dt,
                                   total_min: float, enter_count: int,
                                   short_stay: bool, late_entry: bool,
                                   night_entry: bool, consecutive_late: int) -> None:
    """INSERT ... ON CONFLICT DO UPDATE 写入 participant_daily_stats

    唯一键: (tenant_id, date, name)
    """
    from sqlalchemy import text

    fe_iso = fe_dt.isoformat() if fe_dt else None
    ll_iso = ll_dt.isoformat() if ll_dt else None

    existing = session.execute(text("""
        SELECT id FROM participant_daily_stats
        WHERE tenant_id = :t AND date = :d AND name = :n
        LIMIT 1
    """), {"t": tenant_id, "d": date_str, "n": name}).scalar()

    if existing:
        session.execute(text("""
            UPDATE participant_daily_stats
            SET email = :email, first_entry = :fe, last_leave = :ll,
                total_duration_minutes = :dur, enter_count = :ec,
                short_stay = :ss, late_entry = :le, night_entry = :ne,
                consecutive_late_days = :cl, updated_at = datetime('now')
            WHERE id = :id
        """), {
            "id": existing, "email": email, "fe": fe_iso, "ll": ll_iso,
            "dur": total_min, "ec": enter_count, "ss": int(short_stay),
            "le": int(late_entry), "ne": int(night_entry), "cl": consecutive_late,
        })
    else:
        session.execute(text("""
            INSERT INTO participant_daily_stats
                (tenant_id, date, name, email, first_entry, last_leave,
                 total_duration_minutes, enter_count,
                 short_stay, late_entry, night_entry, consecutive_late_days,
                 updated_at)
            VALUES (:t, :d, :n, :email, :fe, :ll, :dur, :ec,
                    :ss, :le, :ne, :cl, datetime('now'))
        """), {
            "t": tenant_id, "d": date_str, "n": name,
            "email": email, "fe": fe_iso, "ll": ll_iso,
            "dur": total_min, "ec": enter_count,
            "ss": int(short_stay), "le": int(late_entry),
            "ne": int(night_entry), "cl": consecutive_late,
        })


def aggregate_person_stats(
    session: SyncSession = None,
    date_str: str = None,
    tenant_id: str | None = None,
) -> list[dict]:
    """聚合 person_stats + participant_daily_stats，幂等 upsert"""
    close_session = False
    if session is None:
        session = SyncSession()
        close_session = True
    try:
        date_str = date_str or _today_str()
        tenant = tenant_id or settings.default_tenant_id

        from sqlalchemy import text

        names = session.execute(text("""
            SELECT DISTINCT name
            FROM participant_events
            WHERE tenant_id = :t AND date(action_time) = :d
            ORDER BY name
        """), {"t": tenant, "d": date_str}).fetchall()

        results = []
        for (name,) in names:
            email_row = session.execute(text("""
                SELECT email FROM participant_events
                WHERE tenant_id = :t AND date(action_time) = :d AND name = :n AND email != ''
                LIMIT 1
            """), {"t": tenant, "d": date_str, "n": name}).scalar()

            first = session.execute(text("""
                SELECT MIN(action_time) FROM participant_events
                WHERE tenant_id = :t AND date(action_time) = :d AND name = :n AND action = 'enter'
            """), {"t": tenant, "d": date_str, "n": name}).scalar()

            last = session.execute(text("""
                SELECT MAX(action_time) FROM participant_events
                WHERE tenant_id = :t AND date(action_time) = :d AND name = :n AND action = 'leave'
            """), {"t": tenant, "d": date_str, "n": name}).scalar()

            enter_count = session.execute(text("""
                SELECT COUNT(*) FROM participant_events
                WHERE tenant_id = :t AND date(action_time) = :d AND name = :n AND action = 'enter'
            """), {"t": tenant, "d": date_str, "n": name}).scalar() or 0

            total_min = 0.0
            if first and last:
                total_min = (last - first).total_seconds() / 60.0

            email = email_row or ""
            results.append({
                "name": name, "email": email,
                "first_entry": first, "last_leave": last,
                "total_duration_minutes": total_min, "enter_count": enter_count,
            })

        # 幂等 upsert person_stats
        for r in results:
            first_str = r["first_entry"].strftime("%H:%M:%S") if isinstance(r["first_entry"], datetime) else str(r["first_entry"] or "")
            last_str = r["last_leave"].strftime("%H:%M:%S") if isinstance(r["last_leave"], datetime) else str(r["last_leave"] or "")
            _upsert_person_stat(session, tenant, date_str, r["name"],
                                r["email"], first_str, last_str,
                                r["total_duration_minutes"], r["enter_count"])

        # 幂等 upsert participant_daily_stats
        for r in results:
            fe_dt = r["first_entry"]
            if isinstance(fe_dt, str):
                fe_dt = datetime.fromisoformat(fe_dt) if fe_dt else None
            ll_dt = r["last_leave"]
            if isinstance(ll_dt, str):
                ll_dt = datetime.fromisoformat(ll_dt) if ll_dt else None

            short_stay = (r["total_duration_minutes"] < 3.0 and r["enter_count"] > 0) if r["total_duration_minutes"] < 3.0 else False
            late_entry = False
            night_entry = False
            if fe_dt:
                hour = fe_dt.hour
                late_entry = hour > settings.signin_deadline_hour or (
                    hour == settings.signin_deadline_hour and fe_dt.minute > 0
                )
                night_entry = (hour >= 23 or hour < 6)

            consecutive_late = _calc_consecutive_late(session, tenant, r["name"], date_str)

            _upsert_participant_daily_stat(
                session, tenant, date_str, r["name"], r["email"],
                fe_dt, ll_dt, r["total_duration_minutes"], r["enter_count"],
                short_stay, late_entry, night_entry, consecutive_late,
            )

        session.commit()
        return results
    finally:
        if close_session:
            session.close()


def _calc_consecutive_late(session, tenant_id: str, name: str, today_str: str) -> int:
    from sqlalchemy import text
    rows = session.execute(text("""
        SELECT date, late_entry FROM participant_daily_stats
        WHERE tenant_id = :t AND name = :n AND date <= :d
        ORDER BY date DESC
    """), {"t": tenant_id, "n": name, "d": today_str}).fetchall()

    consecutive = 0
    for row_date, was_late in rows:
        if was_late:
            consecutive += 1
        elif row_date == today_str:
            continue
        else:
            break
    return consecutive


def _upsert_hourly_activity(session: SyncSession, tenant_id: str, date_str: str,
                            hour: int, count: int) -> None:
    """INSERT ... ON CONFLICT DO UPDATE 写入 hourly_activity

    唯一键: (tenant_id, date, hour)
    """
    from sqlalchemy import text
    existing = session.execute(text("""
        SELECT id FROM hourly_activity
        WHERE tenant_id = :t AND date = :d AND hour = :h
        LIMIT 1
    """), {"t": tenant_id, "d": date_str, "h": hour}).scalar()

    if existing:
        session.execute(text("""
            UPDATE hourly_activity
            SET person_count = :c
            WHERE id = :id
        """), {"id": existing, "c": count})
    else:
        session.execute(text("""
            INSERT INTO hourly_activity (date, tenant_id, hour, person_count)
            VALUES (:d, :t, :h, :c)
        """), {"d": date_str, "t": tenant_id, "h": hour, "c": count})


def aggregate_hourly_activity(
    session: SyncSession = None,
    date_str: str = None,
    tenant_id: str | None = None,
) -> list[dict]:
    """聚合 hourly_activity — 每小时内活跃人数，幂等 upsert"""
    close_session = False
    if session is None:
        session = SyncSession()
        close_session = True
    try:
        date_str = date_str or _today_str()
        tenant = tenant_id or settings.default_tenant_id

        from sqlalchemy import text

        rows = session.execute(text("""
            SELECT CAST(strftime('%%H', action_time) AS INTEGER) as h,
                   COUNT(DISTINCT name) as cnt
            FROM participant_events
            WHERE tenant_id = :t AND date(action_time) = :d AND action = 'enter'
            GROUP BY h
            ORDER BY h
        """), {"t": tenant, "d": date_str}).fetchall()

        results = []
        for hour, count in rows:
            results.append({"hour": hour, "person_count": count})
            _upsert_hourly_activity(session, tenant, date_str, hour, count)

        session.commit()
        return results
    finally:
        if close_session:
            session.close()


def aggregate_all(
    session: SyncSession = None,
    days_back: int = 7,
    tenant_id: str | None = None,
) -> dict:
    """聚合最近 N 天所有统计"""
    close_session = False
    if session is None:
        session = SyncSession()
        close_session = True
    try:
        dates = _date_range(days_back)
        stats = {"daily": [], "person": [], "hourly": []}
        for d in dates:
            stats["daily"].append(aggregate_daily_stats(session, d, tenant_id=tenant_id))
            p = aggregate_person_stats(session, d, tenant_id=tenant_id)
            if p:
                stats["person"].extend(p)
            stats["hourly"].append(aggregate_hourly_activity(session, d, tenant_id=tenant_id))
        return stats
    finally:
        if close_session:
            session.close()


if __name__ == "__main__":
    result = aggregate_all(days_back=1)
    sys.stdout.write(f"[AGGREGATOR] 聚合完成: daily={len(result['daily'])} "
                     f"person={sum(len(p) if isinstance(p, list) else 1 for p in result['person'])} "
                     f"hourly={len(result['hourly'])}\n")
