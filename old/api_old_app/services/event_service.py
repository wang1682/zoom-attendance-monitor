"""event_service.py — 事件服务
Webhook 事件入队 + participant_events 写入 + alert 生成

Phase 8: 所有函数接受 tenant_id 参数，不再硬编码 default_tenant_id
"""
from __future__ import annotations

import json
from datetime import datetime, timezone

from sqlalchemy import select

from app.models import WebhookEvent, ParticipantEvent, SeenEmail, Alert
from app.database import SyncSession
from app.settings import settings

MYT = timezone.utc  # 时间转换由前端负责，DB 统一存 UTC


def save_webhook_event(
    event_type: str,
    payload: dict,
    tenant_id: str | None = None,
) -> WebhookEvent:
    """保存原始 Webhook 事件"""
    tenant = tenant_id or settings.default_tenant_id
    with SyncSession() as session:
        ev = WebhookEvent(
            tenant_id=tenant,
            event_type=event_type,
            payload=json.dumps(payload, ensure_ascii=False),
        )
        session.add(ev)
        session.commit()
        session.refresh(ev)
        return ev


def save_participant_event(
    meeting_id: str,
    name: str,
    email: str,
    action: str,
    action_time: datetime,
    source: str = "poll",
    tenant_id: str | None = None,
) -> ParticipantEvent:
    """写入参会事件到 participant_events"""
    tenant = tenant_id or settings.default_tenant_id
    with SyncSession() as session:
        from app.models import MonitoredMeeting

        mm = session.execute(
            select(MonitoredMeeting).where(
                MonitoredMeeting.meeting_id == meeting_id,
                MonitoredMeeting.tenant_id == tenant,
            )
        ).scalar_one_or_none()

        if not mm:
            mm = MonitoredMeeting(
                tenant_id=tenant,
                meeting_id=meeting_id,
                label=f"Auto {meeting_id[-4:]}",
            )
            session.add(mm)
            session.flush()
            session.refresh(mm)

        ev = ParticipantEvent(
            tenant_id=tenant,
            meeting_id_ref=mm.id,
            name=name,
            email=email,
            action=action,
            action_time=action_time,
            source=source,
        )
        session.add(ev)
        session.commit()
        session.refresh(ev)
        return ev


def check_new_email(name: str, email: str, now: datetime,
                    tenant_id: str | None = None) -> bool:
    """检查并记录陌生邮箱，返回是否是新人"""
    tenant = tenant_id or settings.default_tenant_id
    if not email:
        return False
    with SyncSession() as session:
        existing = session.execute(
            select(SeenEmail).where(
                SeenEmail.email == email,
                SeenEmail.tenant_id == tenant,
            )
        ).scalar_one_or_none()

        if existing:
            existing.last_seen = now
            existing.seen_count += 1
            session.commit()
            return False

        se = SeenEmail(
            email=email,
            tenant_id=tenant,
            name=name,
            first_seen=now,
            last_seen=now,
        )
        session.add(se)
        session.commit()
        return True


def create_alert(
    alert_type: str,
    title: str,
    message: str = "",
    severity: str = "info",
    related_name: str = "",
    related_email: str = "",
    tenant_id: str | None = None,
) -> Alert:
    """创建告警记录"""
    tenant = tenant_id or settings.default_tenant_id
    with SyncSession() as session:
        a = Alert(
            tenant_id=tenant,
            alert_type=alert_type,
            severity=severity,
            title=title,
            message=message,
            related_name=related_name,
            related_email=related_email,
        )
        session.add(a)
        session.commit()
        session.refresh(a)
        return a
