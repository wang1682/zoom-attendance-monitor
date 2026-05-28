"""api.py — REST API 端点（看板 + 管理）

Phase 8: 所有查询从 request.state.tenant_id 读取租户（由 API Token 中间件注入）
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db
from app.models import DailyStat, PersonStat, HourlyActivity, Alert, ParticipantEvent, SeenEmail
from app.integrations.telegram import TelegramNotifier

router = APIRouter(prefix="/api/v1", tags=["api"])


def _get_tenant_id(request: Request) -> str:
    tid = getattr(request.state, "tenant_id", "default")
    return tid


@router.get("/health")
async def health():
    tg = TelegramNotifier()
    tg_ok = await tg.health()
    return {
        "status": "ok",
        "version": "0.1.0",
        "telegram": tg_ok,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@router.get("/dashboard/today")
async def today_stats(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """今日概览"""
    tenant_id = _get_tenant_id(request)
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    result = await db.execute(
        select(DailyStat).where(
            DailyStat.date == today,
            DailyStat.tenant_id == tenant_id,
        )
    )
    stat = result.scalar_one_or_none()
    if not stat:
        return {"date": today, "message": "今日暂无数据"}
    return {
        "date": stat.date,
        "total_persons": stat.total_persons,
        "total_duration_minutes": stat.total_duration_minutes,
        "earliest_entry": stat.earliest_entry,
        "latest_entry": stat.latest_entry,
        "unique_emails": stat.unique_emails,
    }


@router.get("/dashboard/weekly")
async def weekly_stats(
    request: Request,
    db: AsyncSession = Depends(get_async_db),
):
    """本周趋势"""
    tenant_id = _get_tenant_id(request)
    today = datetime.now(timezone(timedelta(hours=8)))
    week_start = (today - timedelta(days=today.weekday())).strftime("%Y-%m-%d")
    today_str = today.strftime("%Y-%m-%d")

    result = await db.execute(
        select(DailyStat).where(
            DailyStat.tenant_id == tenant_id,
            DailyStat.date >= week_start,
            DailyStat.date < today_str,
        ).order_by(DailyStat.date)
    )
    stats = result.scalars().all()
    return [
        {
            "date": s.date,
            "total_persons": s.total_persons,
            "total_duration_minutes": s.total_duration_minutes,
        }
        for s in stats
    ]


@router.get("/participants/ranking")
async def participant_ranking(
    request: Request,
    days: int = Query(30, description="统计天数"),
    limit: int = Query(10, description="返回人数"),
    db: AsyncSession = Depends(get_async_db),
):
    """参会时长排名"""
    tenant_id = _get_tenant_id(request)
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    start = (datetime.now(timezone(timedelta(hours=8))) - timedelta(days=days)).strftime("%Y-%m-%d")

    result = await db.execute(
        select(
            PersonStat.name,
            func.sum(PersonStat.total_duration_minutes).label("total_dur"),
            func.sum(PersonStat.enter_count).label("total_enters"),
            func.count().label("days_active"),
        ).where(
            PersonStat.tenant_id == tenant_id,
            PersonStat.date >= start,
            PersonStat.date < today,
        ).group_by(PersonStat.name)
        .order_by(func.sum(PersonStat.total_duration_minutes).desc())
        .limit(limit)
    )
    rows = result.all()
    return [
        {
            "rank": i + 1,
            "name": r.name,
            "total_duration_minutes": round(r.total_dur, 1) if r.total_dur else 0,
            "total_enters": r.total_enters or 0,
            "days_active": r.days_active or 0,
        }
        for i, r in enumerate(rows)
    ]


@router.get("/alerts/recent")
async def recent_alerts(
    request: Request,
    limit: int = Query(20),
    db: AsyncSession = Depends(get_async_db),
):
    """最近预警"""
    tenant_id = _get_tenant_id(request)
    result = await db.execute(
        select(Alert)
        .where(Alert.tenant_id == tenant_id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
    )
    alerts = result.scalars().all()
    return [
        {
            "type": a.alert_type,
            "severity": a.severity,
            "title": a.title,
            "message": a.message,
            "name": a.related_name,
            "time": a.created_at.isoformat(),
        }
        for a in alerts
    ]


@router.get("/strangers")
async def strangers_list(
    request: Request,
    limit: int = Query(50),
    db: AsyncSession = Depends(get_async_db),
):
    """陌生邮箱列表"""
    tenant_id = _get_tenant_id(request)
    result = await db.execute(
        select(SeenEmail)
        .where(SeenEmail.tenant_id == tenant_id)
        .order_by(SeenEmail.last_seen.desc())
        .limit(limit)
    )
    emails = result.scalars().all()
    return [
        {
            "email": e.email,
            "name": e.name,
            "first_seen": e.first_seen.isoformat() if hasattr(e.first_seen, 'isoformat') else str(e.first_seen),
            "last_seen": e.last_seen.isoformat() if hasattr(e.last_seen, 'isoformat') else str(e.last_seen),
            "seen_count": e.seen_count,
        }
        for e in emails
    ]


@router.post("/telegram/test")
async def test_telegram():
    """测试 Telegram 推送"""
    tg = TelegramNotifier()
    ok = await tg.send("✅ **测试消息**\nZoom Monitor 推送通道正常")
    return {"sent": ok}
