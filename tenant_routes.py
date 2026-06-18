"""Tenant self-service dashboard routes for Zoom Attendance Monitor.
Mounted as an APIRouter under /dashboard/tenant in the main app.
Tenants see only their own data and can manage channels + alert rules."""

import httpx
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import json
import logging

import db
from config import settings

logger = logging.getLogger(__name__)

router = APIRouter()


# ── Auth helpers ──────────────────────────────────────────────────────────────


def get_current_user(request: Request) -> dict | None:
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get_user_by_id(user_id)
    if not user or not user["is_active"]:
        return None
    user["is_active_str"] = "true" if user["is_active"] else "false"
    return user


async def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


async def require_editor(user: dict = Depends(require_user)) -> dict:
    """Require at least tenant role (viewer cannot modify)."""
    if user.get("role") == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot perform this action")
    return user


# ── Shared nav items builder ──────────────────────────────────────────────────

def _get_nav_items(role: str) -> list[dict]:
    """Build the sidebar nav items filtered by user role.

    super_admin: all items including management
    admin: all items except tenant_admin, system, audit
    tenant_admin: overview, participants, meetings, alerts, push, security
    user: overview, participants, meetings, alerts, security
    """
    items = [
        {"key": "overview",     "label": "总览",   "href": "/dashboard/",                      "icon": ""},
        {"key": "participants", "label": "参会",   "href": "/dashboard/participants",           "icon": ""},
        {"key": "meetings",     "label": "会议",   "href": "/dashboard/meetings",               "icon": ""},
        {"key": "alerts",       "label": "预警",   "href": "/dashboard/alerts",                 "icon": ""},
    ]
    if role in ("super_admin", "admin", "tenant_admin"):
        items += [
            {"key": "channels",     "label": "推送",   "href": "/dashboard/tenant/channels",    "icon": ""},
        ]
    items += [
        {"key": "security",     "label": "安全中心","href": "/dashboard/tenant/security",       "icon": ""},
    ]
    # Management items — 合并到「管理中心」
    if role == "super_admin":
        items += [
            {"key": "accounts",     "label": "账号管理","href": "/dashboard/tenant/accounts",     "icon": ""},
            {"key": "admin_center", "label": "管理中心","href": "/dashboard/admin-center",         "icon": ""},
        ]
    elif role == "admin":
        items += [
            {"key": "accounts",     "label": "账号管理","href": "/dashboard/tenant/accounts",     "icon": ""},
            {"key": "admin_center", "label": "管理中心","href": "/dashboard/admin-center",         "icon": ""},
        ]
    elif role == "tenant_admin":
        items += [
            {"key": "accounts",     "label": "账号管理","href": "/dashboard/tenant/accounts",     "icon": ""},
        ]
    return items


# ── Render helper ─────────────────────────────────────────────────────────────


def _render_tenant(request: Request, active: str, user: dict,
                   template_name: str, **extra) -> HTMLResponse:
    from pathlib import Path
    from fastapi.templating import Jinja2Templates

    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    tenant_id = request.app.state.get_effective_tenant_id(request)

    # 从 DB 重读完整用户资料（确保 telegram 字段最新）
    fresh_user = db.get_user_by_id(user["id"]) or user
    logger.info(
        "tenant zoom page user_id=%s username=%s selected_tenant=%s telegram_2fa=%s",
        fresh_user.get("id"),
        fresh_user.get("username"),
        tenant_id,
        fresh_user.get("telegram_2fa_enabled"),
    )
    current_user = {
        "id": fresh_user["id"],
        "username": fresh_user["username"],
        "display_name": fresh_user.get("display_name", ""),
        "role": fresh_user.get("role", "viewer"),
        "tenant_id": tenant_id,
        "is_active": fresh_user.get("is_active_str", "true" if fresh_user.get("is_active") else "false"),
        "telegram_chat_id": fresh_user.get("telegram_chat_id", ""),
        "telegram_2fa_enabled": fresh_user.get("telegram_2fa_enabled", 0),
        "telegram_2fa_verified_at": fresh_user.get("telegram_2fa_verified_at", ""),
        "twofa_backup_codes": fresh_user.get("twofa_backup_codes", ""),
    }
    tenant_info = db.get_tenant(tenant_id)
    tenant_name = tenant_info.get("display_name", tenant_id) if tenant_info else tenant_id
    role = user.get("role", "user")
    is_viewer = role == "viewer"
    is_super_admin = role == "super_admin"

    # ── 租户切换上下文（仅 super_admin 需要） ──
    if is_super_admin:
        all_tenants = db.get_all_tenants()
        current_tenant = next((t for t in all_tenants if t["id"] == tenant_id), None)
        current_tenant_name = current_tenant["display_name"] if current_tenant else tenant_id
    else:
        all_tenants = []
        current_tenant_name = ""

    hide_settings = role not in ("admin", "super_admin")
    nav_items = _get_nav_items(role)

    context = {
        **extra,
        "request": request,
        "active": active,
        "tenant_name": tenant_name,
        "is_viewer": is_viewer,
        "is_super_admin": is_super_admin,
        "available_tenants": all_tenants,
        "current_tenant_id": tenant_id,
        "current_tenant_name": current_tenant_name,
        "hide_settings": hide_settings,
        "current_user": current_user,
        "nav_items": nav_items,
    }
    return templates.TemplateResponse(request, template_name, context)


# ── Dict helpers ──────────────────────────────────────────────────────────────


def _channel_dict(c: dict) -> dict:
    return {
        "id": c["id"],
        "chat_id": c["chat_id"],
        "label": c.get("label", ""),
        "is_group": "true" if c.get("is_group") else "false",
        "enabled": "true" if c.get("is_enabled") else "false",
    }


def _account_dict(a: dict) -> dict:
    return {
        "id": a["id"],
        "label": a.get("label", ""),
        "account_id": a.get("account_id", ""),
        "host_email": a.get("host_email", ""),
        "status": a.get("status", "inactive"),
        "is_active": a.get("is_active", 1),
        "last_sync": a.get("last_sync", ""),
        "last_sync_result": a.get("last_sync_result", ""),
        "webhook_last_event": a.get("webhook_last_event", ""),
        "webhook_last_time": a.get("webhook_last_time", ""),
    }


# ── Overview (运营面板) ─────────────────────────────────────────────────────


def _compute_kpi_data(tenant_id: str) -> dict:
    """Compute KPI data for tenant dashboard — all queries tenant-isolated."""
    # 今日参与者
    today_participants = len(db.get_today_participants(limit=10000, tenant_id=tenant_id))

    # 当前在线 + 活跃会议 (unified from webhook)
    online_data = db.get_current_online(tenant_id)
    current_online = online_data["online_count"]
    active_meetings = online_data["active_meetings"]

    # 今日事件
    conn = db._get_conn()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM zoom_events WHERE created_at >= ? AND tenant_id = ?",
        (today, tenant_id),
    ).fetchone()
    today_events = row["c"] if row else 0

    # 今日告警
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM alerts WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    today_alerts = row["c"] if row else 0

    # 最近事件 (top 10)
    recent_events = db.get_recent_events(limit=10, tenant_id=tenant_id)

    # 推送状态
    channels = db.get_tenant_channels(tenant_id)
    push_configured = any(c.get("is_enabled") for c in channels)
    push_channel_count = len([c for c in channels if c.get("is_enabled")])

    return {
        "today_participants": today_participants,
        "current_online": current_online,
        "today_events": today_events,
        "today_alerts": today_alerts,
        "recent_events": recent_events,
        "active_meetings": active_meetings,
        "push_configured": push_configured,
        "push_channel_count": push_channel_count,
    }


@router.get("")
@router.get("/")
async def tenant_dashboard_redirect():
    """Redirect old tenant dashboard to unified /dashboard."""
    return RedirectResponse(url="/dashboard", status_code=302)


@router.get("/setup")
async def tenant_setup_redirect():
    """Redirect old tenant setup to unified /dashboard/setup."""
    return RedirectResponse(url="/dashboard/setup", status_code=302)


# ── Zoom 账号自助配置 ──────────────────────────────────────────────────────


@router.get("/security", response_class=HTMLResponse)
async def tenant_security(request: Request, user: dict = Depends(require_user)):
    """Security center page — Telegram 2FA, backup codes."""
    return _render_tenant(
        request, "security", user, "tenant_security.html",
    )


@router.get("/accounts", response_class=HTMLResponse)
async def tenant_accounts(request: Request, user: dict = Depends(require_editor)):
    """Zoom OAuth account management — admin only."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    accounts = db.get_zoom_accounts(tenant_id)
    display_accounts = []
    for a in accounts:
        d = _account_dict(a)
        d["client_id_display"] = a.get("client_id", "")[:8] + "****" if a.get("client_id") else ""
        d["has_client_secret"] = bool(a.get("client_secret"))
        display_accounts.append(d)
    return _render_tenant(
        request, "accounts", user, "tenant_accounts.html",
        accounts=display_accounts,
    )


@router.get("/zoom", response_class=RedirectResponse)
async def tenant_zoom_redirect(request: Request, user: dict = Depends(require_user)):
    """Redirect /zoom to /security for tenant users, /accounts for admin."""
    role = user.get("role", "")
    if role in ("super_admin", "admin"):
        return RedirectResponse(url="/dashboard/tenant/accounts", status_code=302)
    return RedirectResponse(url="/dashboard/tenant/security", status_code=302)


@router.post("/zoom/create", response_class=RedirectResponse)
async def tenant_zoom_create(
    request: Request,
    user: dict = Depends(require_editor),
    label: str = Form(""),
    account_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    webhook_secret: str = Form(""),
):
    tenant_id = request.app.state.get_effective_tenant_id(request)
    db.create_zoom_account(
        tenant_id=tenant_id,
        label=label or account_id,
        account_id=account_id,
        client_id=client_id,
        client_secret=client_secret,
        webhook_secret=webhook_secret,
    )
    return RedirectResponse(url="/dashboard/tenant/zoom", status_code=303)


@router.post("/zoom/{account_db_id}/update", response_class=RedirectResponse)
async def tenant_zoom_update(
    request: Request,
    account_db_id: int,
    user: dict = Depends(require_editor),
    label: str = Form(""),
    account_id: str = Form(...),
    client_id: str = Form(""),
    client_secret: str = Form(""),
    webhook_secret: str = Form(""),
):
    tenant_id = request.app.state.get_effective_tenant_id(request)
    # Verify ownership
    acct = db.get_zoom_account(account_db_id)
    if not acct or str(acct.get("tenant_id")) != str(tenant_id):
        raise HTTPException(status_code=404)
    updates = {
        "label": label or account_id,
        "account_id": account_id,
    }
    if client_id.strip():
        updates["client_id"] = client_id.strip()
    # Only update secrets if user provides a new value
    if client_secret.strip():
        updates["client_secret"] = client_secret.strip()
    if webhook_secret.strip():
        updates["webhook_secret"] = webhook_secret.strip()
    db.update_zoom_account(account_db_id, **updates)
    return RedirectResponse(url="/dashboard/tenant/zoom", status_code=303)


# ── Test connection helpers ──────────────────────────────────────────────────


def _test_result(checks: list, extra: dict = None) -> JSONResponse:
    """Build multi-step test result with rich HTML display."""
    icons = {"ok": "✅", "error": "❌", "warning": "⚠️", "skipped": "⏭️"}
    lines = []
    all_ok = True
    for c in checks:
        ico = icons.get(c["status"], "❓")
        if c["status"] == "error":
            all_ok = False
        lines.append(f"<div style='margin:3px 0'>{ico} <strong>{c['name']}</strong></div>")
        if c.get("detail"):
            lines.append(f"<div style='font-size:0.8rem;color:#94a3b8;padding-left:1.5rem;white-space:pre-wrap'>{c['detail'].replace(chr(10), '<br>')}</div>")
    payload = {"success": all_ok, "html": "\n".join(lines)}
    if extra:
        payload.update(extra)
    return JSONResponse(status_code=200, content=payload)


# ── Test Connection — new credentials (from form) ──


@router.post("/zoom/test")
async def tenant_zoom_test(
    request: Request,
    user: dict = Depends(require_editor),
    account_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    """Test Zoom OAuth connection with provided credentials (multi-step)."""
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            # ── Step 1: OAuth Token ──
            token_url = "https://zoom.us/oauth/token"
            payload = {"grant_type": "account_credentials", "account_id": account_id}
            auth = httpx.BasicAuth(client_id, client_secret)
            tr = await cl.post(token_url, data=payload, auth=auth)
            if tr.status_code != 200:
                return _test_result([
                    {"name": "OAuth Token", "status": "error",
                     "detail": f"HTTP {tr.status_code}: {tr.text[:200]}"},
                ])
            token_data = tr.json()
            access_token = token_data.get("access_token", "")
            checks = [{"name": "OAuth Token", "status": "ok", "detail": "Token 获取成功"}]

            # ── Step 2: User Info ──
            ur = await cl.get(
                "https://api.zoom.us/v2/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if ur.status_code != 200:
                checks.append({"name": "用户信息", "status": "error",
                               "detail": f"HTTP {ur.status_code}"})
                return _test_result(checks)
            ud = ur.json()
            email = ud.get("email", "未知")
            name = f"{ud.get('first_name', '')} {ud.get('last_name', '')}".strip()
            checks.append({"name": "用户信息", "status": "ok",
                           "detail": f"主机: {name} ({email})"})

            # ── Step 3: Metrics/Meetings scope ──
            mr = await cl.get(
                "https://api.zoom.us/v2/metrics/meetings?page_size=1",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if mr.status_code == 200:
                checks.append({"name": "实时会议", "status": "ok",
                               "detail": "Dashboard 权限正常，可读取实时会议列表"})
                # ── Step 4: Participants (conditional) ──
                meetings = mr.json().get("meetings", [])
                if meetings:
                    pr = await cl.get(
                        f"https://api.zoom.us/v2/metrics/meetings/{meetings[0]['id']}/participants?page_size=1",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    if pr.status_code == 200:
                        checks.append({"name": "参会者数据", "status": "ok",
                                       "detail": "参会者数据可读"})
                    else:
                        checks.append({"name": "参会者数据", "status": "warning",
                                       "detail": f"读取失败 (HTTP {pr.status_code})"})
                else:
                    checks.append({"name": "参会者数据", "status": "skipped",
                                   "detail": "当前无活跃会议，自动跳过"})
            elif mr.status_code == 4711:
                checks.append({
                    "name": "实时会议", "status": "error",
                    "detail": "缺少 Dashboard Metrics 权限：dashboard:read:list_meetings:admin\n请在 Zoom S2S App > Scopes 中补充 Dashboard/Metrics 权限后重新授权。",
                })
                checks.append({"name": "参会者数据", "status": "skipped",
                               "detail": "依赖实时会议权限"})
            else:
                checks.append({"name": "实时会议", "status": "error",
                               "detail": f"读取失败 (HTTP {mr.status_code})"})
                checks.append({"name": "参会者数据", "status": "skipped",
                               "detail": "依赖实时会议权限"})

            return _test_result(checks, {"host_email": email})

    except httpx.TimeoutException:
        return _test_result([{"name": "连接", "status": "error",
                              "detail": "连接超时，请检查 Zoom 服务状态"}])
    except Exception as e:
        return _test_result([{"name": "测试", "status": "error",
                              "detail": f"{type(e).__name__}: {str(e)[:200]}"}])


# ── Test Connection — existing account (from DB) ──


@router.post("/zoom/{account_db_id}/test")
async def tenant_zoom_test_existing(
    request: Request,
    account_db_id: int,
    user: dict = Depends(require_editor),
):
    """Test connection for an existing account (multi-step)."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    acct = db.get_zoom_account(account_db_id)
    if not acct or str(acct.get("tenant_id")) != str(tenant_id):
        return _test_result([{"name": "权限", "status": "error",
                              "detail": "账号不存在或无权访问"}])
    if not acct.get("client_id") or not acct.get("client_secret") or not acct.get("account_id"):
        return _test_result([{"name": "凭据", "status": "error",
                              "detail": "账号凭据不完整，请重新编辑"}])
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            # ── Step 1: OAuth Token ──
            token_url = "https://zoom.us/oauth/token"
            payload = {"grant_type": "account_credentials", "account_id": acct["account_id"]}
            auth = httpx.BasicAuth(acct["client_id"], acct["client_secret"])
            tr = await cl.post(token_url, data=payload, auth=auth)
            if tr.status_code != 200:
                return _test_result([
                    {"name": "OAuth Token", "status": "error",
                     "detail": f"HTTP {tr.status_code}"},
                ])
            token_data = tr.json()
            access_token = token_data.get("access_token", "")
            checks = [{"name": "OAuth Token", "status": "ok", "detail": "Token 获取成功"}]

            # ── Step 2: User Info ──
            ur = await cl.get(
                "https://api.zoom.us/v2/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if ur.status_code != 200:
                checks.append({"name": "用户信息", "status": "error",
                               "detail": f"HTTP {ur.status_code}"})
                return _test_result(checks)
            ud = ur.json()
            email = ud.get("email", "未知")
            name = f"{ud.get('first_name', '')} {ud.get('last_name', '')}".strip()
            checks.append({"name": "用户信息", "status": "ok",
                           "detail": f"主机: {name} ({email})"})

            # ── Step 3: Metrics/Meetings scope ──
            mr = await cl.get(
                "https://api.zoom.us/v2/metrics/meetings?page_size=1",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if mr.status_code == 200:
                checks.append({"name": "实时会议", "status": "ok",
                               "detail": "Dashboard 权限正常，可读取实时会议列表"})
                # ── Step 4: Participants (conditional) ──
                meetings = mr.json().get("meetings", [])
                if meetings:
                    pr = await cl.get(
                        f"https://api.zoom.us/v2/metrics/meetings/{meetings[0]['id']}/participants?page_size=1",
                        headers={"Authorization": f"Bearer {access_token}"},
                    )
                    if pr.status_code == 200:
                        checks.append({"name": "参会者数据", "status": "ok",
                                       "detail": "参会者数据可读"})
                    else:
                        checks.append({"name": "参会者数据", "status": "warning",
                                       "detail": f"读取失败 (HTTP {pr.status_code})"})
                else:
                    checks.append({"name": "参会者数据", "status": "skipped",
                                   "detail": "当前无活跃会议，自动跳过"})
            elif mr.status_code == 4711:
                checks.append({
                    "name": "实时会议", "status": "error",
                    "detail": "缺少 Dashboard Metrics 权限：dashboard:read:list_meetings:admin\n请在 Zoom S2S App > Scopes 中补充 Dashboard/Metrics 权限后重新授权。",
                })
                checks.append({"name": "参会者数据", "status": "skipped",
                               "detail": "依赖实时会议权限"})
            else:
                checks.append({"name": "实时会议", "status": "error",
                               "detail": f"读取失败 (HTTP {mr.status_code})"})
                checks.append({"name": "参会者数据", "status": "skipped",
                               "detail": "依赖实时会议权限"})

            # Persist status if all basic steps succeeded
            all_ok = all(c["status"] == "ok" for c in checks if c["name"] != "参会者数据")
            if all_ok:
                from datetime import datetime, timezone
                db.update_zoom_account(
                    account_db_id,
                    status="active",
                    host_email=email,
                    last_sync=datetime.now(timezone.utc).isoformat(),
                    last_sync_result="OK",
                )

            return _test_result(checks, {"host_email": email if all_ok else None})

    except httpx.TimeoutException:
        return _test_result([{"name": "连接", "status": "error",
                              "detail": "连接超时"}])
    except Exception as e:
        return _test_result([{"name": "测试", "status": "error",
                              "detail": f"{str(e)[:200]}"}])


@router.post("/zoom/{account_db_id}/delete")
async def tenant_zoom_delete(
    request: Request,
    account_db_id: int,
    user: dict = Depends(require_editor),
):
    tenant_id = request.app.state.get_effective_tenant_id(request)
    acct = db.get_zoom_account(account_db_id)
    if not acct or str(acct.get("tenant_id")) != str(tenant_id):
        raise HTTPException(status_code=404)
    db.delete_zoom_account(account_db_id)
    return RedirectResponse(url="/dashboard/tenant/zoom", status_code=303)


@router.post("/zoom/{account_db_id}/set-active")
async def tenant_zoom_set_active(
    request: Request,
    account_db_id: int,
    user: dict = Depends(require_editor),
):
    tenant_id = request.app.state.get_effective_tenant_id(request)
    acct = db.get_zoom_account(account_db_id)
    if not acct or str(acct.get("tenant_id")) != str(tenant_id):
        return _render_tenant(request, "zoom", user, "tenant_zoom.html",
                              zoom_accounts=db.get_zoom_accounts(tenant_id),
                              error="账号不存在或不属于当前租户")
    # 该租户下所有账号 is_active=0，目标账号 is_active=1
    conn = db._get_conn()
    conn.execute("UPDATE zoom_accounts SET is_active = 0 WHERE tenant_id = ?", (tenant_id,))
    conn.execute("UPDATE zoom_accounts SET is_active = 1 WHERE id = ?", (account_db_id,))
    conn.commit()
    db.log_audit("update", "zoom_account", account_db_id,
                 f"Set zoom account {acct.get('account_id','')} as active for tenant {tenant_id}")
    return RedirectResponse(url="/dashboard/tenant/zoom", status_code=303)


# ── Telegram 频道管理 ────────────────────────────────────────────────────────


@router.get("/channels", response_class=HTMLResponse)
async def tenant_channels_page(request: Request, user: dict = Depends(require_user)):
    tenant_id = request.app.state.get_effective_tenant_id(request)
    channels = [_channel_dict(c) for c in db.get_tenant_channels(tenant_id)]
    bot_config = db.get_tenant_bot_config(tenant_id)
    return _render_tenant(request, "channels", user, "tenant_channels.html",
                          channels=channels, bot_config=bot_config)


@router.post("/channels/create")
async def tenant_channels_create(request: Request,
                                  chat_id: str = Form(...),
                                  label: str = Form(""),
                                  is_group: str = Form("false"),
                                  bot_token: str = Form(""),
                                  bot_username: str = Form(""),
                                  user: dict = Depends(require_editor)):
    tenant_id = request.app.state.get_effective_tenant_id(request)
    db.create_tenant_channel(tenant_id, chat_id.strip(), label.strip(), is_group == "true",
                             bot_token.strip(), bot_username.strip())
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


@router.post("/channels/{channel_id}/toggle")
async def tenant_channels_toggle(request: Request, channel_id: int,
                                  user: dict = Depends(require_editor)):
    db.toggle_tenant_channel(channel_id)
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


@router.post("/channels/{channel_id}/delete")
async def tenant_channels_delete(request: Request, channel_id: int,
                                  user: dict = Depends(require_editor)):
    db.delete_tenant_channel(channel_id)
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


@router.post("/channels/{channel_id}/edit")
async def tenant_channels_edit(request: Request, channel_id: int,
                                chat_id: str = Form(...),
                                label: str = Form(""),
                                is_group: str = Form("false"),
                                bot_token: str = Form(""),
                                bot_username: str = Form(""),
                                user: dict = Depends(require_editor)):
    """Edit a channel."""
    conn = db._get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE tenant_channels SET chat_id=?, label=?, is_group=?, "
        "bot_token=?, bot_username=?, updated_at=? WHERE id=?",
        (chat_id.strip(), label.strip(), 1 if is_group == "true" else 0,
         bot_token.strip(), bot_username.strip(), now, channel_id)
    )
    conn.commit()
    db.log_audit("update", "channel", channel_id,
                 f"Updated channel: {label.strip()} (chat_id={chat_id.strip()})")
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


@router.post("/channels/{channel_id}/test")
async def tenant_channels_test(request: Request, channel_id: int,
                                user: dict = Depends(require_editor)):
    """Send a test push to the given channel. Uses channel's own bot if configured."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    channels = db.get_tenant_channels(tenant_id)
    target = next((c for c in channels if c["id"] == channel_id), None)
    if not target:
        return JSONResponse({"ok": False, "error": "Channel not found"}, status_code=404)
    # Use channel's bot_token first, fallback to tenant's bot, then global
    token = target.get("bot_token", "") or db.get_tenant_bot_config(tenant_id)["token"] or settings.telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": target["chat_id"],
                "text": "✅ 测试消息 — 推送配置正常，机器人已接入",
            })
            data = resp.json()
            if data.get("ok"):
                return JSONResponse({"ok": True, "message": "✅ 测试消息已发送", "chat_id": target["chat_id"]})
            else:
                err = data.get("description", "Telegram API returned error")
                return JSONResponse({"ok": False, "error": err, "chat_id": target["chat_id"]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/channels/bot-test")
async def tenant_bot_test(request: Request,
                           bot_token: str = Form(...),
                           user: dict = Depends(require_editor)):
    """Test a bot token via getMe, return bot info."""
    url = f"https://api.telegram.org/bot{bot_token.strip()}/getMe"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            data = resp.json()
            if data.get("ok"):
                bot = data["result"]
                return JSONResponse({
                    "ok": True,
                    "id": bot.get("id"),
                    "username": bot.get("username", ""),
                    "first_name": bot.get("first_name", ""),
                })
            return JSONResponse({"ok": False, "error": data.get("description", "getMe failed")})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.post("/channels/bot-config")
async def tenant_bot_config_save(request: Request,
                                   bot_token: str = Form(""),
                                   user: dict = Depends(require_editor)):
    """Save tenant's bot token. If empty, do not overwrite (edit-safe)."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    token = bot_token.strip()
    if not token:
        # Edit-safe: empty means "don't change"
        return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)
    # Verify via getMe
    url = f"https://api.telegram.org/bot{token}/getMe"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            data = resp.json()
            if data.get("ok"):
                bot = data["result"]
                username = bot.get("username", "")
                db.update_tenant_bot_config(tenant_id, token, username,
                                              datetime.utcnow().isoformat())
            else:
                # Token invalid — still save but with empty username (show as broken)
                db.update_tenant_bot_config(tenant_id, token)
    except Exception:
        db.update_tenant_bot_config(tenant_id, token)
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


@router.post("/channels/bot-config/clear")
async def tenant_bot_config_clear(request: Request,
                                    user: dict = Depends(require_editor)):
    """Clear tenant's bot config."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    db.update_tenant_bot_config(tenant_id, "", "", "")
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


# ── 告警规则开关 ───────────────────────────────────────────────────────────────


@router.get("/alerts", response_class=HTMLResponse)
async def tenant_alerts_page(request: Request, user: dict = Depends(require_user)):
    tenant_id = request.app.state.get_effective_tenant_id(request)
    rules = db.get_telegram_rules_by_tenant(tenant_id)
    channels = db.get_tenant_channels(tenant_id)
    bot_config = db.get_tenant_bot_config(tenant_id)
    bot_username = "系统"
    if bot_config.get("username"):
        bot_username = "@" + bot_config["username"]
    return _render_tenant(request, "alerts", user, "tenant_alerts.html",
                          rules=rules, channels=channels,
                          bot_username=bot_username)


@router.post("/alerts/toggle")
async def tenant_alerts_toggle(request: Request,
                                event_type: str = Form(...),
                                enabled: int = Form(...),
                                target_chat_id: str = Form(""),
                                user: dict = Depends(require_editor)):
    """Toggle a single alert rule on/off and/or rebind channel."""
    db.upsert_telegram_rule(event_type, {
        "enabled": enabled,
        "target_chat_id": target_chat_id or "",
    })
    return RedirectResponse(url="/dashboard/tenant/alerts", status_code=303)


# ── Setup Status API ──────────────────────────────────────────────────────────


async def _compute_setup_status(tenant_id: str) -> dict:
    """Compute setup readiness score and checks for a tenant.

    Async — queries Zoom API for live meeting data.
    """
    checks = {}

    # 1. Zoom account configured (20 pts)
    accounts = db.get_zoom_accounts(tenant_id)
    has_account = any(a.get("is_active") and a.get("client_id") for a in accounts)
    checks["zoom_account"] = has_account

    # 2. OAuth verified (15 pts) — account status == 'active'
    has_oauth = any(
        a.get("is_active") and a.get("status") == "active"
        for a in accounts
    )
    checks["oauth"] = bool(has_oauth)

    # 3. Meetings data (10 pts) — Zoom API has active meetings with participants
    try:
        accounts = db.get_zoom_accounts(tenant_id) if hasattr(db, 'get_zoom_accounts') else []
        active = next(
            (a for a in accounts if a.get("is_active") and a.get("status") == "active"),
            None,
        )
        if active:
            from zoom_metrics import ZoomMetrics
            zm = ZoomMetrics(active)
        else:
            from zoom_metrics import ZoomMetrics
            zm = ZoomMetrics()
        live_data = await zm.get_live()
        meetings = live_data.get("meetings", [])
        has_active_meetings = any(
            m.get("participants") and len(m.get("participants", [])) > 0
            for m in meetings
        )
    except Exception:
        # Tenant Zoom account may lack Metrics API scope — fallback to global
        try:
            from zoom_metrics import ZoomMetrics
            zm_global = ZoomMetrics()
            live_data = await zm_global.get_live()
            meetings = live_data.get("meetings", [])
            has_active_meetings = any(
                m.get("participants") and len(m.get("participants", [])) > 0
                for m in meetings
            )
        except Exception:
            has_active_meetings = False
    checks["meetings"] = has_active_meetings

    # 4. Participants data (15 pts)
    conn = db._get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM zoom_participants WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    participant_count = row["c"] if row else 0
    checks["participants"] = participant_count > 0

    # 5. Webhook recent 24h (15 pts)
    from datetime import datetime, timedelta, timezone
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM zoom_events WHERE created_at >= ? AND tenant_id = ?",
        (cutoff, tenant_id),
    ).fetchone()
    webhook_count = row["c"] if row else 0
    checks["webhook"] = webhook_count > 0

    # 6. Telegram configured (10 pts)
    channels = db.get_tenant_channels(tenant_id)
    has_telegram = any(c.get("is_enabled") for c in channels)
    checks["telegram"] = bool(has_telegram)

    # 7. Member mapping rate (10 pts)
    all_rows = conn.execute(
        "SELECT COUNT(*) AS c FROM member_display WHERE tenant_id = ?",
        (tenant_id,),
    ).fetchone()
    unmapped = conn.execute(
        "SELECT COUNT(*) AS c FROM member_display "
        "WHERE tenant_id = ? AND display_name = raw_name",
        (tenant_id,),
    ).fetchone()
    total_members = all_rows["c"] if all_rows else 0
    unmapped_count = unmapped["c"] if unmapped else 0
    if total_members > 0:
        mapped_rate = (total_members - unmapped_count) / total_members
    else:
        mapped_rate = 0
    checks["member_mapping"] = round(mapped_rate, 2)

    # 8. No duplicates (5 pts)
    dup_rows = conn.execute(
        "SELECT display_name, COUNT(*) as c FROM member_display "
        "WHERE tenant_id = ? GROUP BY display_name HAVING c > 1",
        (tenant_id,),
    ).fetchall()
    checks["duplicates"] = len(dup_rows)

    # Score calculation
    weights = {
        "zoom_account": 20,
        "oauth": 15,
        "meetings": 10,
        "participants": 15,
        "webhook": 15,
        "telegram": 10,
        "member_mapping": 10,
        "duplicates": 5,
    }
    score = 0
    for key, weight in weights.items():
        if key == "member_mapping" and isinstance(checks[key], (int, float)):
            score += int(weight * checks[key])
        elif key == "duplicates" and checks[key] == 0:
            score += weight
        elif isinstance(checks[key], bool) and checks[key]:
            score += weight

    if score >= 80:
        status_label = "good"
    elif score >= 50:
        status_label = "partial"
    else:
        status_label = "poor"

    next_steps = []
    if not checks["zoom_account"]:
        next_steps.append("配置 Zoom S2S OAuth → 前往接入中心开始配置")
    if not checks["oauth"] and checks["zoom_account"]:
        next_steps.append("完成 OAuth 授权验证")
    if not checks["webhook"]:
        next_steps.append("配置 Webhook（接收实时事件）")
    if not checks["telegram"]:
        next_steps.append("创建推送频道")
    if isinstance(checks["member_mapping"], (int, float)) and checks["member_mapping"] < 0.8:
        next_steps.append("完成成员映射（当前 {}%）".format(int(checks["member_mapping"] * 100)))

    return {
        "score": score,
        "status": status_label,
        "checks": checks,
        "next_steps": next_steps,
    }


@router.get("/api/setup/status")
async def setup_status(request: Request, user: dict = Depends(require_user)):
    """Setup Center readiness score for the current tenant."""
    tenant_id = request.app.state.get_effective_tenant_id(request)


# ═══════════════════════════════════════════
# Telegram 2FA 设置
# ═══════════════════════════════════════════


@router.post("/settings/2fa/bind")
async def bind_telegram_2fa(request: Request,
                            user: dict = Depends(require_user),
                            chat_id: str = Form(...)):
    """Step 1: Send a 6-digit confirmation code to the given chat_id."""
    chat_id = chat_id.strip()
    if not chat_id:
        return {"success": False, "error": "Chat ID 不能为空"}
    if not chat_id.lstrip("-").isdigit():
        return {"success": False, "error": "Chat ID 格式错误，请输入纯数字"}
    from config import settings
    from telegram_push import send_message
    import secrets as sec

    tenant_id = request.app.state.get_effective_tenant_id(request)
    bot_cfg = db.get_tenant_bot_config(tenant_id)
    token = bot_cfg.get("token") or settings.telegram_bot_token

    code = f"{sec.randbelow(1000000):06d}"

    result = send_message(
        f"🔐 两步验证绑定确认\n\n确认码：{code}\n\n请在 Dashboard 输入该确认码完成绑定。验证码 5 分钟内有效。",
        chat_id=chat_id,
        bot_token=token
    )
    if not result.get("ok"):
        msg = result.get("error", "")
        hint = ""
        if "chat not found" in msg.lower():
            hint = " — 请先向 Bot 发送 /start"
        elif "forbidden" in msg.lower():
            hint = " — Bot 被用户拉黑"
        return {"success": False, "error": f"无法发送消息到此 Chat ID: {msg}{hint}"}

    # 存储确认码（在 app.state 中，过期时间 5 分钟）
    # 以 chat_id 为 key，方便 webhook 通过 chat_id 查到绑定关系
    import time as _time
    if not hasattr(request.app.state, "_2fa_bind_pending"):
        request.app.state._2fa_bind_pending = {}
    request.app.state._2fa_bind_pending[chat_id] = {
        "user_id": user["id"],
        "code": code,
        "expires_at": _time.time() + 300,
    }
    db.log_security_event("bind_telegram_2fa_sent_code", username=user["username"],
                          tenant_id=request.app.state.get_effective_tenant_id(request),
                          result="success")
    return {"success": True, "sent": True}


@router.post("/settings/2fa/bind/confirm")
async def confirm_bind_2fa(request: Request,
                           user: dict = Depends(require_user),
                           code: str = Form(...)):
    """Step 2: Verify the confirmation code and bind the Telegram chat_id."""
    code = code.strip()
    if not code:
        return {"success": False, "error": "确认码不能为空"}
    if not hasattr(request.app.state, "_2fa_bind_pending"):
        return {"success": False, "error": "未找到待绑定的确认码，请重新发送"}
    import time as _time
    # 遍历查找 user_id 对应的 pending（key 现在是 chat_id）
    pending = None
    pending_chat_id = None
    for cid, p in request.app.state._2fa_bind_pending.items():
        if p.get("user_id") == user["id"]:
            pending = p
            pending_chat_id = cid
            break
    if not pending:
        return {"success": False, "error": "未找到待绑定的确认码，请先发送确认码"}
    if _time.time() > pending["expires_at"]:
        request.app.state._2fa_bind_pending.pop(pending_chat_id, None)
        return {"success": False, "error": "确认码已过期，请重新发送"}
    if code != pending["code"]:
        return {"success": False, "error": "确认码错误"}

    chat_id = pending_chat_id
    db.set_user_telegram_chat_id(user["id"], chat_id)
    db.enable_telegram_2fa(user["id"])
    request.app.state._2fa_bind_pending.pop(pending_chat_id, None)
    db.log_security_event("bind_telegram_2fa", username=user["username"],
                          tenant_id=request.app.state.get_effective_tenant_id(request),
                          result="success")

    # 再次通知用户
    from config import settings
    from telegram_push import send_message
    tenant_id = request.app.state.get_effective_tenant_id(request)
    bot_cfg = db.get_tenant_bot_config(tenant_id)
    token = bot_cfg.get("token") or settings.telegram_bot_token
    send_message("✅ Telegram 已绑定，两步验证已启用。\n下次登录将需要 Telegram 验证码。", chat_id=chat_id, bot_token=token)

    return {"success": True}


@router.post("/settings/2fa/unbind")
async def unbind_telegram_2fa(request: Request,
                              user: dict = Depends(require_user)):
    """Unbind Telegram and disable 2FA."""
    db.disable_telegram_2fa(user["id"])
    db.log_security_event("unbind_telegram_2fa", username=user["username"], result="success")
    return {"success": True}


@router.post("/settings/2fa/enable")
async def enable_telegram_2fa(request: Request,
                              user: dict = Depends(require_user)):
    """Enable Telegram 2FA — requires bound chat_id."""
    from config import settings
    full_user = db.get_user_by_id(user["id"])
    if not full_user or not full_user.get("telegram_chat_id"):
        return {"success": False, "error": "请先绑定 Telegram 账号"}
    codes = db.generate_backup_codes(8)
    db.save_backup_codes(user["id"], codes)
    db.enable_telegram_2fa(user["id"])
    from telegram_push import send_message

    # 使用租户级 bot token，而非全局 token
    tenant_id = request.app.state.get_effective_tenant_id(request)
    bot_cfg = db.get_tenant_bot_config(tenant_id)
    token = bot_cfg.get("token") or settings.telegram_bot_token

    send_message(
        "✅ 两步验证已启用\n\n登录时请使用 Telegram 验证码。\n\n备用码已生成，请妥善保管。",
        chat_id=full_user["telegram_chat_id"],
        bot_token=token
    )
    db.log_security_event("enable_telegram_2fa", username=user["username"], result="success")
    return {"success": True, "backup_codes": codes}


@router.post("/settings/2fa/disable")
async def disable_telegram_2fa(request: Request,
                               user: dict = Depends(require_user),
                               password: str = Form(...)):
    """Disable Telegram 2FA — requires current password verification."""
    # 验证当前用户密码
    from db import verify_user_password
    if not verify_user_password(user["username"], password):
        db.log_security_event("disable_2fa_failed", user_id=user["id"],
                              username=user["username"], result="failed",
                              details="密码验证失败")
        return {"success": False, "error": "密码错误，操作已记录"}
    db.disable_telegram_2fa(user["id"])
    db.log_security_event("disable_telegram_2fa", user_id=user["id"],
                          username=user["username"], result="success")
    return {"success": True}


@router.post("/settings/2fa/backup-codes")
async def get_backup_codes(request: Request,
                           user: dict = Depends(require_user)):
    """Return current backup codes."""
    full_user = db.get_user_by_id(user["id"])
    if not full_user or not full_user.get("twofa_backup_codes"):
        return {"success": False, "error": "无备用码"}
    return {"success": True, "codes": json.loads(full_user["twofa_backup_codes"])}


@router.post("/settings/2fa/backup-codes/regenerate")
async def regenerate_backup_codes(request: Request,
                                  user: dict = Depends(require_user)):
    """Regenerate and save new backup codes."""
    codes = db.generate_backup_codes(8)
    db.save_backup_codes(user["id"], codes)
    db.log_security_event("regenerate_backup_codes", username=user["username"], result="success")
    return {"success": True, "codes": codes}

# ═══════════════════════════════════════════
# Telegram Bot Webhook — /start chat ID
# ═══════════════════════════════════════════

@router.post("/webhook/telegram/{tenant_id}")
async def telegram_bot_webhook(request: Request, tenant_id: str):
    """Interactive Telegram bot webhook — menu, Chat ID, and bind 2FA."""
    from telegram_push import send_message
    import time as _time

    body = await request.json()
    message = body.get("message", {})
    chat = message.get("chat", {})
    from_user = message.get("from", {})

    # ── callback_query 优先处理（不依赖顶层 chat_id） ──────────────
    if body.get("callback_query"):
        cb = body["callback_query"]
        cb_data = cb.get("data", "")
        cb_chat = cb.get("message", {}).get("chat", {})
        cb_chat_id = str(cb_chat.get("id", ""))
        cb_from = cb.get("from", {})

        if not cb_chat_id:
            return {"ok": False, "error": "No chat_id in callback_query"}

        bot_cfg = db.get_tenant_bot_config(tenant_id)
        bot_token = bot_cfg.get("token") or ""
        if not bot_token:
            return {"ok": False, "error": "Tenant bot not configured"}

        import httpx

        if cb_data == "chatid":
            username = cb_from.get("username") or cb_from.get("first_name", "未知")
            first_name = cb_from.get("first_name", "")
            last_name = cb_from.get("last_name", "")
            full_name = f"{first_name} {last_name}".strip() or username
            reply = (
                "🆔 当前 Telegram 信息\n\n"
                f"Chat ID:\n{cb_chat_id}\n\n"
                f"用户名:\n@{username}\n\n"
                f"名称:\n{full_name}\n\n"
                "可复制 Chat ID 到 Dashboard 完成绑定。"
            )
            send_message(reply, chat_id=cb_chat_id, bot_token=bot_token)
            httpx.post(f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                       json={"callback_query_id": cb["id"]})
            return {"ok": True, "handled": "cb_chatid"}

        elif cb_data == "bind":
            reply = (
                "🔗 Dashboard 绑定\n\n"
                "请先登录：\n"
                "https://zoom.dhbwang.com/dashboard/zoom\n\n"
                "在 Dashboard 点击「发送确认码」\n"
                "然后将收到的 6 位确认码发送给我\n\n"
                "例如：\n"
                "123456"
            )
            send_message(reply, chat_id=cb_chat_id, bot_token=bot_token)
            httpx.post(f"https://api.telegram.org/bot{bot_token}/answerCallbackQuery",
                       json={"callback_query_id": cb["id"]})
            return {"ok": True, "handled": "cb_bind"}

        return {"ok": True}

    chat_id = str(chat.get("id", ""))
    text = (message.get("text") or "").strip()

    print(f"[webhook] tenant={tenant_id} chat_id={chat_id} text={text}")

    if not chat_id:
        return {"ok": False, "error": "No chat_id"}

    bot_cfg = db.get_tenant_bot_config(tenant_id)
    bot_token = bot_cfg.get("token") or ""
    if not bot_token:
        return {"ok": False, "error": "Tenant bot not configured"}

    # ── /start — 主菜单 ──────────────────────────────────────────────
    if text == "/start":
        reply = (
            "🔐 Zoom Monitor 安全中心\n\n"
            "欢迎使用两步验证服务\n\n"
            "🆔 我的 Chat ID\n"
            "🔗 绑定 Dashboard 2FA\n\n"
            "请选择功能："
        )
        # Inline keyboard via reply markup
        import json as _json
        menu = _json.dumps({
            "inline_keyboard": [
                [
                    {"text": "🆔 我的 Chat ID", "callback_data": "chatid"},
                    {"text": "🔗 绑定 Dashboard 2FA", "callback_data": "bind"},
                ]
            ]
        })
        send_message(reply, chat_id=chat_id, bot_token=bot_token, reply_markup=menu)
        print(f"[webhook] menu sent to {chat_id}")
        return {"ok": True, "handled": "menu"}

    # ── /chatid — 显示用户信息 ──────────────────────────────────────
    if text == "/chatid":
        username = from_user.get("username") or from_user.get("first_name", "未知")
        first_name = from_user.get("first_name", "")
        last_name = from_user.get("last_name", "")
        full_name = f"{first_name} {last_name}".strip() or username
        reply = (
            "🆔 当前 Telegram 信息\n\n"
            f"Chat ID:\n{chat_id}\n\n"
            f"用户名:\n@{username}\n\n"
            f"名称:\n{full_name}\n\n"
            "可复制 Chat ID 到 Dashboard 完成绑定。"
        )
        send_message(reply, chat_id=chat_id, bot_token=bot_token)
        print(f"[webhook] chatid sent to {chat_id}")
        return {"ok": True, "handled": "chatid"}
    if text.isdigit() and len(text) == 6:
        if not hasattr(request.app.state, "_2fa_bind_pending"):
            reply = "❌ 没有待绑定的确认码，请先在 Dashboard 点击「发送确认码」。"
            send_message(reply, chat_id=chat_id, bot_token=bot_token)
            return {"ok": True, "handled": "no_pending"}

        pending = request.app.state._2fa_bind_pending.get(chat_id)
        if not pending:
            reply = "❌ 未找到对应此 Chat ID 的绑定请求，请先在 Dashboard 点击「发送确认码」。"
            send_message(reply, chat_id=chat_id, bot_token=bot_token)
            return {"ok": True, "handled": "not_found"}

        if _time.time() > pending["expires_at"]:
            request.app.state._2fa_bind_pending.pop(chat_id, None)
            reply = "⏰ 确认码已过期，请重新在 Dashboard 点击「发送确认码」。"
            send_message(reply, chat_id=chat_id, bot_token=bot_token)
            return {"ok": True, "handled": "expired"}

        if text != pending["code"]:
            reply = "❌ 确认码错误，请重试。"
            send_message(reply, chat_id=chat_id, bot_token=bot_token)
            return {"ok": True, "handled": "wrong_code"}

        # ✅ 验证通过 — 绑定
        user_id = pending["user_id"]
        db.set_user_telegram_chat_id(user_id, chat_id)
        request.app.state._2fa_bind_pending.pop(chat_id, None)

        # 查用户信息
        user = db.get_user_by_id(user_id)
        username = user.get("username", "?") if user else "?"

        reply = (
            "✅ 绑定成功\n\n"
            f"账户：{tenant_id}\n"
            f"Telegram：{chat_id}\n\n"
            "两步验证已关联。\n"
            "请返回 Dashboard 启用两步验证。"
        )
        send_message(reply, chat_id=chat_id, bot_token=bot_token)
        print(f"[webhook] bind success: user={user_id} chat_id={chat_id}")
        return {"ok": True, "handled": "bound"}

    # ── 其他消息 ────────────────────────────────────────────────────
    reply = "请发送 /start 查看菜单"
    send_message(reply, chat_id=chat_id, bot_token=bot_token)
    return {"ok": True, "chat_id": chat_id}
