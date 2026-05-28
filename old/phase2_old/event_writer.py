"""
phase2/event_writer.py — 事件写入服务

ZoomEvent / ZoomParticipant / AlertLog 写入口
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from phase2.db import SyncSession, new_id
from phase2.models import ZoomEvent, ZoomParticipant, AlertLog
from app.settings import settings


def write_zoom_event(
    event_type: str,
    meeting_id: str = "",
    participant_name: str = "",
    participant_email: str = "",
    raw_payload: dict = None,
    processed: bool = False,
    tenant_id: str = "default",
) -> ZoomEvent:
    """写入 Webhook 事件到 zoom_events"""
    with SyncSession() as s:
        ev = ZoomEvent(
            id=new_id(),
            tenant_id=tenant_id,
            event_type=event_type,
            meeting_id=meeting_id,
            participant_name=participant_name,
            participant_email=participant_email,
            raw_payload=raw_payload or {},
            processed=processed,
            received_at=datetime.now(timezone.utc),
        )
        s.add(ev)
        s.commit()
        return ev


def write_participant(
    meeting_id: str,
    name: str,
    email: str,
    action: str,           # enter / leave
    action_time: datetime,
    duration_seconds: int = 0,
    source: str = "poll",
    tenant_id: str = "default",
) -> ZoomParticipant:
    """写入参会记录到 zoom_participants"""
    with SyncSession() as s:
        p = ZoomParticipant(
            id=new_id(),
            tenant_id=tenant_id,
            meeting_id=meeting_id,
            name=name,
            email=email,
            action=action,
            action_time=action_time,
            duration_seconds=duration_seconds,
            source=source,
        )
        s.add(p)
        s.commit()
        return p


def write_alert_log(
    channel: str = "telegram",
    message_type: str = "",
    title: str = "",
    content: str = "",
    recipient: str = "",
    success: bool = True,
    error_message: str = "",
    tenant_id: str = "default",
) -> AlertLog:
    """写入推送日志到 alert_logs"""
    with SyncSession() as s:
        log = AlertLog(
            id=new_id(),
            tenant_id=tenant_id,
            channel=channel,
            message_type=message_type,
            title=title,
            content=content[:500],       # 截断，不存全量
            recipient=recipient,
            success=success,
            error_message=error_message,
            sent_at=datetime.now(timezone.utc),
        )
        s.add(log)
        s.commit()
        return log
