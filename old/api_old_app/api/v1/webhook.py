"""
api.py — Webhook 接收端点

落库 zoom_events / zoom_participants 双写
"""
from __future__ import annotations

import hashlib
import hmac
import json
from datetime import datetime, timezone

from fastapi import APIRouter, Request, HTTPException, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import settings
from app.database import get_async_db
from app.services.event_service import save_webhook_event, save_participant_event
from phase2.event_writer import write_zoom_event, write_participant

router = APIRouter(prefix="/webhook", tags=["webhook"])


def verify_signature(payload: bytes, signature: str) -> bool:
    """Zoom Webhook 签名验证"""
    if not settings.zoom_webhook_secret or not signature:
        return False
    expected = hmac.new(
        settings.zoom_webhook_secret.encode(),
        payload,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"v0={expected}", signature)


@router.post("/zoom")
async def zoom_webhook(request: Request, db: AsyncSession = Depends(get_async_db)):
    """接收 Zoom Webhook 事件"""
    body = await request.body()
    sig = request.headers.get("x-zm-signature", "")

    # 验证签名
    if settings.zoom_webhook_secret:
        if not verify_signature(body, sig):
            raise HTTPException(status_code=403, detail="Invalid signature")

    payload = json.loads(body)
    event_type = payload.get("event", "")
    obj = payload.get("payload", {}).get("object", {})
    meeting_id = str(obj.get("id", ""))
    participant = obj.get("participant", {})
    p_name = participant.get("user_name", "").strip()
    p_email = participant.get("email", "") or participant.get("user_email", "")

    # === Phase 1: 原始结构落库（保持兼容）===
    save_webhook_event(event_type, payload, tenant_id=settings.default_tenant_id)

    # === Phase 2: zoom_events 落库 ===
    write_zoom_event(
        event_type=event_type,
        meeting_id=meeting_id,
        participant_name=p_name,
        participant_email=p_email,
        raw_payload=payload,
        processed=(event_type in ("meeting.participant_joined", "meeting.participant_left")),
        tenant_id=settings.default_tenant_id,
    )

    # 解析参会事件
    if event_type in ("meeting.participant_joined", "meeting.participant_left"):
        join_time = participant.get("join_time", "")
        leave_time = participant.get("leave_time", "")

        if event_type == "meeting.participant_joined" and join_time:
            action_time = datetime.fromisoformat(join_time.replace("Z", "+00:00"))
            # Phase 1 兼容
            save_participant_event(meeting_id, p_name, p_email, "enter", action_time, source="webhook",
                                  tenant_id=settings.default_tenant_id)
            # Phase 2 zoom_participants
            write_participant(
                meeting_id=meeting_id, name=p_name, email=p_email,
                action="enter", action_time=action_time, source="webhook",
                tenant_id=settings.default_tenant_id,
            )

        elif event_type == "meeting.participant_left" and leave_time:
            action_time = datetime.fromisoformat(leave_time.replace("Z", "+00:00"))
            save_participant_event(meeting_id, p_name, p_email, "leave", action_time, source="webhook",
                                  tenant_id=settings.default_tenant_id)
            write_participant(
                meeting_id=meeting_id, name=p_name, email=p_email,
                action="leave", action_time=action_time, source="webhook",
                tenant_id=settings.default_tenant_id,
            )

    return {"status": "ok"}
