"""
phase2/audit.py — 审计日志写入

写 audit_logs 表，记录所有安全事件。
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from fastapi import Request

from phase2.db import SyncSession
from phase2.models import AuditLog


def log_audit(
    action: str,
    username: str = "",
    request: Request | None = None,
    detail: str = "",
    tenant_id: str | None = None,
) -> str:
    """写入一条审计日志，返回 ID

    action: login_success / login_failed / logout / tenant_created 等
    tenant_id: 显式传入，否则从 request session 或默认 'default'
    """
    audit_id = uuid4().hex[:16]
    ip = ""
    ua = ""
    _tenant_id = tenant_id or "default"

    # 尝试从请求 session 中推断 tenant_id
    if request and not tenant_id:
        from phase2.auth import get_current_user
        session = get_current_user(request)
        if session:
            _tenant_id = session.get("tenant_id", "default")

    if request:
        forwarded = request.headers.get("x-forwarded-for", "")
        ip = forwarded.split(",")[0].strip() if forwarded else (request.client.host if request.client else "")
        ua = request.headers.get("user-agent", "")[:512]

    with SyncSession() as s:
        s.add(AuditLog(
            id=audit_id,
            tenant_id=_tenant_id,
            action=action,
            username=username,
            ip_address=ip,
            user_agent=ua,
            detail=detail,
            occurred_at=datetime.now(timezone.utc),
        ))
        s.commit()
    return audit_id
