"""
phase2/admin.py — P8 多租户管理 API + Dashboard 页面

Tenants / ZoomAccounts / DashboardUsers 的 CRUD 管理。
所有路由挂载在 /dashboard/admin/ 下，仅 owner 角色可操作。
"""
from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request, Form, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse
import jinja2

from phase2.db import SyncSession, new_id
from phase2.models import (
    Tenant, ZoomAccount, ZoomMeeting, ZoomParticipant, ZoomEvent,
    DashboardUser, TelegramChannel, AlertRule, AlertLog, SystemSetting, AuditLog,
)
from phase2.auth import (
    get_current_user, read_session, set_session_cookie, clear_session_cookie,
    check_role, check_owner_last,
)
from phase2.models import ROLE_HIERARCHY, ROLE_SCOPES
from phase2.audit import log_audit

router = APIRouter()

TEMPLATE_DIR = str(Path(__file__).parent.parent / "templates")

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
)
_jinja_env.filters['from_json'] = lambda s: json.loads(s) if s else {}


def _render(name: str, **ctx) -> HTMLResponse:
    template = _jinja_env.get_template(name)
    html = template.render(**ctx)
    return HTMLResponse(html)


def _admin_check(request: Request) -> dict | None:
    """检查登录 + 返回 session user dict"""
    user = get_current_user(request)
    if not user:
        return None
    return user


def _check_role(request: Request, min_role: str = "admin") -> bool:
    """通用角色检查：session user 的 role >= min_role

    Args:
        request: FastAPI 请求（用于读 cookie）
        min_role: 最低所需角色，默认 admin

    Returns:
        True=通过，False=拒绝
    """
    user = get_current_user(request)
    if not user:
        return False
    username = user.get("user", "")

    # .env admin 无条件通过（超级管理员）
    from app.settings import settings
    if username == settings.dashboard_admin_user:
        return True

    # 查 DB user role
    with SyncSession() as s:
        dbuser = s.query(DashboardUser).filter_by(
            username=username,
            tenant_id=user.get("tenant_id", "default"),
        ).first()
        if dbuser and ROLE_HIERARCHY.get(dbuser.role, 0) >= ROLE_HIERARCHY.get(min_role, 0):
            return True
    return False


def _serialize(rows):
    result = []
    for r in rows:
        d = {}
        for col in r.__table__.columns:
            val = getattr(r, col.name)
            if hasattr(val, 'isoformat'):
                val = val.isoformat()
            elif val is None:
                val = ""
            elif isinstance(val, bool):
                val = str(val).lower()
            d[col.name] = val
        result.append(d)
    return result


# ═══════════════════════════════════════════════
# Tenant 管理
# ═══════════════════════════════════════════════

@router.get("/dashboard/admin/tenants", response_class=HTMLResponse)
async def admin_tenants_list(request: Request):
    user = _admin_check(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    if not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail="tried to access admin/tenants, role insufficient")
        return _render("error.html", request=request, status_code=403,
            title="权限不足", message="仅 owner 可管理租户")

    with SyncSession() as s:
        tenants = s.query(Tenant).order_by(Tenant.created_at.desc()).all()

    return _render("admin_tenants.html",
        request=request, active="admin", subactive="tenants",
        tenants=_serialize(tenants),
        current_user=user,
    )


@router.post("/dashboard/admin/tenants/create")
async def admin_tenants_create(
    request: Request,
    name: str = Form(...),
    display_name: str = Form(""),
    plan: str = Form("free"),
):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail=f"tried to create tenant, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")

    with SyncSession() as s:
        tid = new_id()
        s.add(Tenant(
            id=tid, name=name,
            display_name=display_name or name,
            plan=plan, active=True,
        ))
        s.commit()
        log_audit("tenant_created", username=user.get("user"),
                   detail=f"tenant={tid}, name={name}")

    return RedirectResponse(url="/dashboard/admin/tenants", status_code=302)


@router.post("/dashboard/admin/tenants/{tid}/toggle")
async def admin_tenants_toggle(request: Request, tid: str):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail=f"tried to toggle tenant {tid}, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")

    with SyncSession() as s:
        tenant = s.query(Tenant).filter_by(id=tid).first()
        if tenant:
            tenant.active = not tenant.active
            s.commit()
            log_audit("tenant_toggled", username=user.get("user"),
                       detail=f"tenant={tid}, active={tenant.active}")

    return RedirectResponse(url="/dashboard/admin/tenants", status_code=302)


@router.post("/dashboard/admin/tenants/{tid}/delete")
async def admin_tenants_delete(request: Request, tid: str):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail=f"tried to delete tenant {tid}, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")
    if tid == "default":
        raise HTTPException(status_code=400, detail="不能删除默认租户")

    with SyncSession() as s:
        s.query(Tenant).filter_by(id=tid).delete()
        s.commit()
        log_audit("tenant_deleted", username=user.get("user"),
                   detail=f"tenant={tid}")

    return RedirectResponse(url="/dashboard/admin/tenants", status_code=302)


@router.post("/dashboard/admin/tenants/{tid}/switch")
async def admin_tenants_switch(request: Request, tid: str):
    """切换当前 session 的 tenant_id（UI 顶部选择器）"""
    user = _admin_check(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=302)

    with SyncSession() as s:
        tenant = s.query(Tenant).filter_by(id=tid, active=True).first()
        if not tenant:
            raise HTTPException(status_code=404, detail="租户不存在或已停用")

    # 重新签发 session cookie
    username = user.get("user", "admin")
    resp = RedirectResponse(url="/dashboard", status_code=302)
    set_session_cookie(resp, username, tenant_id=tid)

    log_audit("tenant_switch", username=username, detail=f"switch_to={tid}",
              tenant_id=tid)
    return resp


# ═══════════════════════════════════════════════
# ZoomAccount 管理
# ═══════════════════════════════════════════════

@router.get("/dashboard/admin/accounts", response_class=HTMLResponse)
async def admin_accounts_list(request: Request):
    user = _admin_check(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    if not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail="tried to access admin/accounts, role insufficient")
        return _render("error.html", request=request, status_code=403,
            title="权限不足", message="仅 owner 可管理账号")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        accounts = s.query(ZoomAccount).filter_by(
            tenant_id=tenant_id
        ).order_by(ZoomAccount.created_at.desc()).all()
        meetings = s.query(ZoomMeeting).filter_by(
            tenant_id=tenant_id
        ).order_by(ZoomMeeting.created_at.desc()).all()

    return _render("admin_accounts.html",
        request=request, active="admin", subactive="accounts",
        accounts=_serialize(accounts),
        meetings=_serialize(meetings),
        current_user=user,
    )


@router.post("/dashboard/admin/accounts/create")
async def admin_accounts_create(
    request: Request,
    label: str = Form(""),
    account_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    host_email: str = Form(""),
):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail="tried to create admin/accounts, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        aid = new_id()
        s.add(ZoomAccount(
            id=aid, tenant_id=tenant_id,
            label=label or f"账号{account_id[:8]}",
            account_id=account_id,
            client_id=client_id,
            client_secret=client_secret,
            host_email=host_email,
            active=True,
        ))
        s.commit()

    return RedirectResponse(url="/dashboard/admin/accounts", status_code=302)


@router.post("/dashboard/admin/accounts/{aid}/delete")
async def admin_accounts_delete(request: Request, aid: str):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail=f"tried to delete account {aid}, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        s.query(ZoomAccount).filter_by(id=aid, tenant_id=tenant_id).delete()
        s.commit()

    return RedirectResponse(url="/dashboard/admin/accounts", status_code=302)


@router.post("/dashboard/admin/meetings/create")
async def admin_meetings_create(
    request: Request,
    meeting_id: str = Form(...),
    label: str = Form(""),
    meeting_type: str = Form("pmi"),
):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail="tried to create meeting, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        mid = new_id()
        s.add(ZoomMeeting(
            id=mid, tenant_id=tenant_id,
            meeting_id=meeting_id,
            label=label or f"会议{meeting_id[-4:]}",
            meeting_type=meeting_type,
            active=True,
        ))
        s.commit()

    return RedirectResponse(url="/dashboard/admin/accounts", status_code=302)


@router.post("/dashboard/admin/meetings/{mid}/delete")
async def admin_meetings_delete(request: Request, mid: str):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail=f"tried to delete meeting {mid}, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        s.query(ZoomMeeting).filter_by(id=mid, tenant_id=tenant_id).delete()
        s.commit()

    return RedirectResponse(url="/dashboard/admin/accounts", status_code=302)


# ═══════════════════════════════════════════════
# DashboardUser 管理
# ═══════════════════════════════════════════════

@router.get("/dashboard/admin/users", response_class=HTMLResponse)
async def admin_users_list(request: Request):
    user = _admin_check(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    if not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail="tried to access admin/users, role insufficient")
        return _render("error.html", request=request, status_code=403,
            title="权限不足", message="仅 owner 可管理用户")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        users = s.query(DashboardUser).filter_by(
            tenant_id=tenant_id
        ).order_by(DashboardUser.created_at.desc()).all()
        tenants = s.query(Tenant).all()

    return _render("admin_users.html",
        request=request, active="admin", subactive="users",
        users=_serialize(users),
        tenants=_serialize(tenants),
        current_user=user,
    )


@router.post("/dashboard/admin/users/create")
async def admin_users_create(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    display_name: str = Form(""),
    role: str = Form("viewer"),
):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail="tried to create user, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")

    import bcrypt
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        existing = s.query(DashboardUser).filter_by(
            username=username, tenant_id=tenant_id
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="用户名已存在")

        uid = new_id()
        s.add(DashboardUser(
            id=uid, tenant_id=tenant_id,
            username=username,
            password_hash=hashed,
            display_name=display_name or username,
            role=role,
            active=True,
        ))
        s.commit()

    return RedirectResponse(url="/dashboard/admin/users", status_code=302)


@router.post("/dashboard/admin/users/{uid}/toggle")
async def admin_users_toggle(request: Request, uid: str):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        log_audit("forbidden", username=user.get("user",""),
                   detail=f"tried to toggle user {uid}, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        dbuser = s.query(DashboardUser).filter_by(
            id=uid, tenant_id=tenant_id
        ).first()
        if dbuser:
            dbuser.active = not dbuser.active
            s.commit()

    return RedirectResponse(url="/dashboard/admin/users", status_code=302)


@router.post("/dashboard/admin/users/{uid}/delete")
async def admin_users_delete(request: Request, uid: str):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        raise HTTPException(status_code=403, detail="Forbidden")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        s.query(DashboardUser).filter_by(id=uid, tenant_id=tenant_id).delete()
        s.commit()

    return RedirectResponse(url="/dashboard/admin/users", status_code=302)


# ═══════════════════════════════════════════════
# Telegram Channel 管理（按当前 tenant）
# ═══════════════════════════════════════════════

@router.get("/dashboard/admin/channels", response_class=HTMLResponse)
async def admin_channels_list(request: Request):
    user = _admin_check(request)
    if user is None:
        return RedirectResponse(url="/login", status_code=302)
    if not _check_role(request, "admin"):
        log_audit("forbidden", username=user.get("user",""),
                   detail=f"tried to access admin/channels, role insufficient")
        return _render("error.html", request=request, status_code=403,
            title="权限不足", message="需要 admin 及以上角色")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        channels = s.query(TelegramChannel).filter_by(
            tenant_id=tenant_id
        ).order_by(TelegramChannel.created_at.desc()).all()

    return _render("admin_channels.html",
        request=request, active="admin", subactive="channels",
        channels=_serialize(channels),
        current_user=user,
    )


@router.post("/dashboard/admin/channels/create")
async def admin_channels_create(
    request: Request,
    chat_id: str = Form(...),
    label: str = Form(""),
    is_group: str = Form("false"),
):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        raise HTTPException(status_code=403, detail="Forbidden")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        cid = new_id()
        s.add(TelegramChannel(
            id=cid, tenant_id=tenant_id,
            chat_id=chat_id,
            label=label or f"频道{chat_id[-4:]}",
            is_group=is_group.lower() == "true",
            enabled=True,
        ))
        s.commit()

    return RedirectResponse(url="/dashboard/admin/channels", status_code=302)


@router.post("/dashboard/admin/channels/{chid}/toggle")
async def admin_channels_toggle(request: Request, chid: str):
    user = _admin_check(request)
    if user is None or not _check_role(request, "admin"):
        log_audit("forbidden", username=user.get("user",""),
                   detail="tried to toggle admin/channels, role insufficient")
        raise HTTPException(status_code=403, detail="Forbidden")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        ch = s.query(TelegramChannel).filter_by(
            id=chid, tenant_id=tenant_id
        ).first()
        if ch:
            ch.enabled = not ch.enabled
            s.commit()

    log_audit("channel_toggle", username=user.get("user",""),
               detail=f"channel={chid}")
    return RedirectResponse(url="/dashboard/admin/channels", status_code=302)


@router.post("/dashboard/admin/channels/{chid}/delete")
async def admin_channels_delete(request: Request, chid: str):
    user = _admin_check(request)
    if user is None or not _check_role(request, "owner"):
        raise HTTPException(status_code=403, detail="Forbidden")

    tenant_id = user.get("tenant_id", "default")
    with SyncSession() as s:
        s.query(TelegramChannel).filter_by(
            id=chid, tenant_id=tenant_id
        ).delete()
        s.commit()

    return RedirectResponse(url="/dashboard/admin/channels", status_code=302)
