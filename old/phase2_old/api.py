"""
phase2/api.py — Phase 2 Dashboard API（安全版）

所有 /api/v2/* 路由（除 /health 外）都需要认证：
- 浏览器访问：session cookie（来自 /login）
- 程序访问：Authorization: Bearer <token>
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Query
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import text

from phase2.db import SyncSession
from phase2.models import (
    ZoomEvent, ZoomParticipant, AlertLog, AlertRule,
    TelegramChannel, ZoomMeeting, ZoomAccount,
)
from phase2.auth import require_auth, get_current_user
from app.integrations.telegram import TelegramNotifier

router = APIRouter(prefix="/api/v2", tags=["phase2"])

MYT = timezone(timedelta(hours=8))

# Bearer Token 支持（OpenAPI 自动显示 Authorize 按钮）
_bearer_scheme = HTTPBearer(auto_error=False)

# 公开路由集合（无需认证）
PUBLIC_PATHS = {"/api/v2/health", "/api/v2/"}


async def _verify_api_auth(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme),
) -> str:
    """API 路由认证依赖

    1. 优先检查 session cookie（浏览器访问）
    2. 其次检查 Bearer Token（程序访问）
    3. /health 始终公开

    返回用户名，未认证抛出 401
    """
    path = request.url.path.rstrip("/")
    if path in PUBLIC_PATHS:
        return "anonymous"

    # 1. session cookie
    user = get_current_user(request)
    if user:
        return user

    # 2. Bearer Token
    if credentials:
        from app.settings import settings
        token = credentials.credentials
        if settings.api_token and token == settings.api_token:
            return "api_token"

    # 3. 未认证
    raise HTTPException(
        status_code=401,
        detail="Unauthorized — login via /login or provide Authorization: Bearer <token>",
        headers={"WWW-Authenticate": "Bearer"},
    )


def _today_start_end() -> tuple[datetime, datetime]:
    """返回今天 MYT 00:00:00 ~ 23:59:59 的 naive UTC datetime（用于 SQLite 比较）"""
    MYT = timezone(timedelta(hours=8))
    now_myt = datetime.now(MYT)
    start = now_myt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = now_myt.replace(hour=23, minute=59, second=59, microsecond=999999)
    # 转 UTC naive（因为 DB 存的是 naive UTC）
    start_utc = start.astimezone(timezone.utc).replace(tzinfo=None)
    end_utc = end.astimezone(timezone.utc).replace(tzinfo=None)
    return start_utc, end_utc


# ─── 公开路由 ────────────────────────────

@router.get("/health")
async def v2_health(auth_user: str = Depends(_verify_api_auth)):
    """Phase 2 健康检查（公开）"""
    tg = TelegramNotifier()
    tg_ok = await tg.health()
    with SyncSession() as s:
        counts = {}
        for tbl in ("zoom_events", "zoom_participants", "alert_logs", "alert_rules", "telegram_channels"):
            cnt = s.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
            counts[tbl] = cnt
        tables = s.execute(
            text("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")
        ).scalars().all()

    return {
        "status": "ok",
        "version": "0.2.0",
        "telegram": tg_ok,
        "tables": tables,
        "record_counts": counts,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ─── 需认证路由 ─────────────────────────

@router.get("/events/today")
async def events_today(
    limit: int = Query(50),
    auth_user: str = Depends(_verify_api_auth),
):
    """今日 Zoom Webhook 事件"""
    start, end = _today_start_end()
    with SyncSession() as s:
        rows = s.query(ZoomEvent).filter(
            ZoomEvent.received_at >= start,
            ZoomEvent.received_at <= end,
        ).order_by(ZoomEvent.received_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "event_type": r.event_type,
            "meeting_id": r.meeting_id,
            "participant_name": r.participant_name,
            "processed": r.processed,
            "received_at": r.received_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/participants/today")
async def participants_today(
    limit: int = Query(100),
    auth_user: str = Depends(_verify_api_auth),
):
    """今日参会记录"""
    start, end = _today_start_end()
    with SyncSession() as s:
        rows = s.query(ZoomParticipant).filter(
            ZoomParticipant.action_time >= start,
            ZoomParticipant.action_time <= end,
        ).order_by(ZoomParticipant.action_time.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "meeting_id": r.meeting_id,
            "name": r.name,
            "email": r.email,
            "action": r.action,
            "action_time": r.action_time.isoformat(),
            "duration_seconds": r.duration_seconds,
            "source": r.source,
        }
        for r in rows
    ]


@router.get("/alerts/today")
async def alerts_today(
    limit: int = Query(50),
    auth_user: str = Depends(_verify_api_auth),
):
    """今日推送/预警日志"""
    start, end = _today_start_end()
    with SyncSession() as s:
        rows = s.query(AlertLog).filter(
            AlertLog.sent_at >= start,
            AlertLog.sent_at <= end,
        ).order_by(AlertLog.sent_at.desc()).limit(limit).all()
    return [
        {
            "id": r.id,
            "channel": r.channel,
            "message_type": r.message_type,
            "title": r.title,
            "recipient": r.recipient,
            "success": r.success,
            "error_message": r.error_message,
            "sent_at": r.sent_at.isoformat(),
        }
        for r in rows
    ]


@router.get("/summary/today")
async def summary_today(auth_user: str = Depends(_verify_api_auth)):
    """今日汇总（一行看全貌）"""
    start, end = _today_start_end()
    with SyncSession() as s:
        event_cnt = s.query(ZoomEvent).filter(
            ZoomEvent.received_at >= start,
            ZoomEvent.received_at <= end,
        ).count()

        enter_cnt = s.query(ZoomParticipant).filter(
            ZoomParticipant.action_time >= start,
            ZoomParticipant.action_time <= end,
            ZoomParticipant.action == "enter",
        ).count()
        leave_cnt = s.query(ZoomParticipant).filter(
            ZoomParticipant.action_time >= start,
            ZoomParticipant.action_time <= end,
            ZoomParticipant.action == "leave",
        ).count()

        unique = s.query(ZoomParticipant.name).filter(
            ZoomParticipant.action_time >= start,
            ZoomParticipant.action_time <= end,
        ).distinct().count()

        meeting_ids = s.query(ZoomParticipant.meeting_id).filter(
            ZoomParticipant.action_time >= start,
            ZoomParticipant.action_time <= end,
        ).distinct().all()
        meeting_cnt = len(meeting_ids)

        push_cnt = s.query(AlertLog).filter(
            AlertLog.sent_at >= start,
            AlertLog.sent_at <= end,
        ).count()
        push_ok = s.query(AlertLog).filter(
            AlertLog.sent_at >= start,
            AlertLog.sent_at <= end,
            AlertLog.success == True,
        ).count()
        push_fail = push_cnt - push_ok

    return {
        "date": datetime.now(MYT).strftime("%Y-%m-%d"),
        "events": event_cnt,
        "participants": {"enter": enter_cnt, "leave": leave_cnt, "unique_people": unique},
        "active_meetings": meeting_cnt,
        "pushes": {"total": push_cnt, "success": push_ok, "failed": push_fail},
    }
