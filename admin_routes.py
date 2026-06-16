"""
Multi-tenant admin dashboard routes for Zoom Attendance Monitor.
Mounted as an APIRouter under /dashboard in the main app.
"""
import json
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Form, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
from config import settings
from zoom_api import ZoomAPI

router = APIRouter()


# ── Auth helpers ──────────────────────────────────────────────────────────────

def get_current_user(request: Request) -> dict | None:
    """Extract user info from session. Returns None if not logged in."""
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    user = db.get_user_by_id(user_id)
    if not user or not user["is_active"]:
        return None
    # Format boolean fields
    user["is_active_str"] = "true" if user["is_active"] else "false"
    return user


async def require_user(request: Request) -> dict:
    """Dependency: redirect to login if not authenticated."""
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


# ── Template helpers ──────────────────────────────────────────────────────────

def _tenant_dict(t: dict) -> dict:
    """Convert SQLite row to template-friendly dict with string booleans."""
    return {
        "id": t["id"],
        "name": t["name"],
        "display_name": t["display_name"] or t["name"],
        "plan": t["plan"],
        "active": "true" if t.get("is_active") else "false",
        "is_global_admin": "true" if t.get("is_global_admin") else "false",
        "api_token": t.get("api_token", ""),
        "created_at": t.get("created_at", ""),
    }


def _user_dict(u: dict) -> dict:
    """Convert user row to template-friendly dict."""
    return {
        "id": u["id"],
        "username": u["username"],
        "display_name": u.get("display_name", ""),
        "role": u.get("role", "viewer"),
        "active": "true" if u.get("is_active") else "false",
        "created_at": u.get("created_at", ""),
    }


def _account_dict(a: dict) -> dict:
    """Convert zoom_account row to template-friendly dict."""
    return {
        "id": a["id"],
        "label": a.get("label", ""),
        "tenant_id": a.get("tenant_id", ""),
        "account_id": a.get("account_id", ""),
        "host_email": a.get("host_email", ""),
        "client_id": a.get("client_id", ""),
        "webhook_secret": a.get("webhook_secret", ""),
        "status": a.get("status", "inactive"),
        "is_active": a.get("is_active", 1),
        "last_sync": a.get("last_sync", ""),
        "last_sync_result": a.get("last_sync_result", ""),
        "webhook_last_event": a.get("webhook_last_event", ""),
        "webhook_last_time": a.get("webhook_last_time", ""),
        "created_at": a.get("created_at", ""),
    }


def _meeting_dict(m: dict) -> dict:
    """Convert meeting row to template-friendly dict."""
    return {
        "id": m["id"],
        "meeting_id": m["meeting_id"],
        "label": m.get("label", ""),
        "meeting_type": m.get("meeting_type", "pmi"),
        "active": "true" if m.get("is_active") else "false",
    }


def _channel_dict(c: dict) -> dict:
    """Convert channel row to template-friendly dict."""
    return {
        "id": c["id"],
        "chat_id": c["chat_id"],
        "label": c.get("label", ""),
        "is_group": "true" if c.get("is_group") else "false",
        "enabled": "true" if c.get("is_enabled") else "false",
    }


# ── GET routes ────────────────────────────────────────────────────────────────

@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard_index(request: Request, user: dict = Depends(require_user)):
    compute_fn = request.app.state.compute_kpi_data
    tenant_id = request.app.state.get_effective_tenant_id(request)
    recent_alerts = db.get_recent_alerts(limit=5, tenant_id=tenant_id)
    # score/checks from DB (sync, no Zoom API call)
    score = 0
    checks = {}
    accounts = db.get_zoom_accounts(tenant_id)
    has_account = any(a.get("is_active") and a.get("client_id") for a in accounts)
    checks["zoom_account"] = has_account
    active_accts = [a for a in accounts if a.get("is_active") and a.get("status") == "active"]
    checks["oauth"] = len(active_accts) > 0
    checks["meetings"] = len(active_accts) > 0
    checks["participants"] = len(active_accts) > 0
    checks["webhook"] = any(a.get("host_email") for a in active_accts)
    channels = db.get_tenant_channels(tenant_id)
    checks["telegram"] = any(c.get("is_enabled") for c in channels)
    score = sum(1 for v in checks.values() if v) * 20
    kpi = await compute_fn(tenant_id)
    return _render_admin(request, "overview", user, "dashboard.html",
                         kpi=kpi, active_meetings=kpi.get("active_meetings", []),
                         score=score, checks=checks,
                         recent_alerts=recent_alerts,
                         participants=kpi.get("participants", []),
                         next_steps=[])

@router.get("/events", response_class=HTMLResponse)
async def dashboard_events_page(request: Request, user: dict = Depends(require_user)):
    """Unified events page under /dashboard/events — paginated, searchable, tenant-isolated."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    page = int(request.query_params.get("page", 1))
    search = request.query_params.get("search", "")
    type_filter = request.query_params.get("type", "")

    events, total, total_pages = db.get_events_paginated(
        tenant_id, page=page, per_page=50,
        search=search, event_type=type_filter,
    )
    event_types = db.get_distinct_event_types(tenant_id)
    tenant = db.get_tenant(tenant_id)
    tenant_name = tenant.get("display_name", tenant_id) if tenant else tenant_id

    # Pagination range for template
    max_page = max(total_pages, 1)
    page_start = max(1, page - 2)
    page_end = min(max_page, page + 2)
    page_range = list(range(page_start, page_end + 1))

    return _render_admin(request, "events", user, "events.html",
                         events=events, total=total,
                         total_pages=total_pages, page=page,
                         event_types=event_types, type_filter=type_filter,
                         search=search, tenant_name=tenant_name,
                         page_range=page_range)


@router.get("/participants", response_class=HTMLResponse)
async def dashboard_participants(request: Request, user: dict = Depends(require_user)):
    """Admin dashboard 成员中心 — 租户可见。
    
    在线状态使用实时 live meetings 数据（Zoom Metrics API），
    今日累计数据（进入、离开、时长、首次/最后活动）用今日考勤汇总。
    """
    role = user.get("role", "")

    from db import get_today_attendance_summary, get_all_groups

    tenant_id = request.app.state.get_effective_tenant_id(request)

    # ── 参数 ──
    search = request.query_params.get("search", "").strip()
    group_filter = request.query_params.get("group", "").strip()
    status_filter = request.query_params.get("status", "").strip()  # "online" or "offline"

    # ── 获取当前在线名单（live source） ──
    online_names = set()
    try:
        from zoom_metrics import ZoomMetrics
        from db import get_zoom_accounts
        accounts = get_zoom_accounts(tenant_id)
        active = next(
            (a for a in accounts if a.get("is_active") and a.get("status") == "active"),
            None,
        )
        if active:
            zm = ZoomMetrics(active)
            live_data = await zm.get_live()
            meetings = live_data.get("meetings", [])
            for m in meetings:
                for p in m.get("participants", []):
                    name = p.get("name", "").strip()
                    if name:
                        online_names.add(name)
    except Exception:
        pass  # 静默失败，online_names 为空则全部离线

    # ── 今日考勤汇总（累计数据） ──
    summary = get_today_attendance_summary(tenant_id=tenant_id)
    members = summary.get("members", [])

    # ── 用 live 名单覆盖在线状态 ──
    for m in members:
        sn = m.get("standard_name", "")
        m["status"] = "online" if sn in online_names else "offline"
    # 按 live 数据计算在线/离线数量
    live_online = len(online_names)
    live_offline = sum(1 for m in members if m.get("status") == "offline")

    # ── 搜索 / 筛选 ──
    if search:
        q = search.lower()
        members = [m for m in members if q in m.get("standard_name", "").lower()]
    if group_filter:
        members = [m for m in members if m.get("group_name", "") == group_filter]
    if status_filter:
        members = [m for m in members if m.get("status", "") == status_filter]

    # ── 分组列表（用于筛选器） ──
    all_groups = get_all_groups(tenant_id)

    # ── member_display 映射（按 tenant 隔离；super_admin 不过滤） ──
    conn = db._get_conn()
    if tenant_id:
        md_rows = conn.execute(
            "SELECT raw_name, display_name, aliases, note, count_enabled, group_id FROM member_display WHERE tenant_id = ?",
            (tenant_id,)
        ).fetchall()
    else:
        md_rows = conn.execute(
            "SELECT raw_name, display_name, aliases, note, count_enabled, group_id FROM member_display"
        ).fetchall()
    member_displays = {}
    for r in md_rows:
        raw_name = r[0]
        aliases = json.loads(r[2]) if r[2] else []
        member_displays[raw_name] = {
            "display_name": r[1],
            "aliases": aliases,
            "note": r[3] or "",
            "count_enabled": bool(r[4]),
            "group_id": r[5],
        }

    return _render_admin(request, "participants", user, "participants.html",
                         title="总管理成员中心" if role in ("admin", "super_admin") else "租户成员中心",
                         members=members,
                         total_members=summary.get("total_members", 0),
                         online_count=live_online,
                         offline_count=live_offline,
                         total_duration=summary.get("total_duration", "0m"),
                         avg_duration=summary.get("avg_duration", "0m"),
                         date=summary.get("date", ""),
                         groups=all_groups,
                         search=search,
                         group_filter=group_filter,
                         status_filter=status_filter,
                         member_displays=json.dumps(member_displays),
                         tenant_id=tenant_id)


@router.get("/meetings", response_class=HTMLResponse)
async def dashboard_meetings(request: Request, user: dict = Depends(require_user)):
    """Meetings center — live meetings from Zoom Metrics API + history + sharing."""
    from db import get_meeting_history, get_sharing_records, get_zoom_accounts
    from zoom_metrics import ZoomMetrics

    tenant_id = request.app.state.get_effective_tenant_id(request)
    tab = request.query_params.get("tab", "live")

    # ── Live meetings from Zoom API (同源 /api/v3/live) ──
    live = []
    try:
        accounts = get_zoom_accounts(tenant_id)
        active = next(
            (a for a in accounts if a.get("is_active") and a.get("status") == "active"),
            None,
        )
        if active:
            zm = ZoomMetrics(active)
            live_data = await zm.get_live()
            for m in live_data.get("meetings", []):
                participants = m.get("participants", [])
                last_join = max(
                    (p.get("join_time", "") for p in participants if p.get("join_time")),
                    default="",
                )
                live.append({
                    "topic": m.get("meeting_topic", "Untitled"),
                    "meeting_id": m.get("meeting_id", ""),
                    "participant_count": len(participants),
                    "online_count": sum(1 for p in participants if p.get("status") == "in_meeting"),
                    "last_activity": last_join,
                })
    except Exception:
        pass  # live stays empty

    history, total_meetings = get_meeting_history(tenant_id, limit=100, offset=0)
    sharing = get_sharing_records(tenant_id, limit=100)

    return _render_admin(request, "meetings", user, "meetings.html",
                         title="会议中心",
                         live_meetings=live,
                         history_meetings=history,
                         total_meetings=total_meetings,
                         sharing_records=sharing,
                         tab=tab)


@router.get("/alerts", response_class=HTMLResponse)
async def dashboard_alerts_page(request: Request, user: dict = Depends(require_user)):
    """Alert rules page — tenant-isolated, unified under /dashboard/alerts."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    rules = db.get_rules_with_channels(tenant_id)
    channels = db.get_tenant_channels(tenant_id)
    return _render_admin(request, "alerts", user, "tenant_alerts.html",
                         rules=rules, channels=channels)


@router.get("/settings", response_class=HTMLResponse)
async def dashboard_settings(request: Request, user: dict = Depends(require_user)):
    """Settings page — redirect to Zoom config for current tenant."""
    return RedirectResponse(url="/dashboard/zoom", status_code=303)


@router.get("/setup")
async def dashboard_setup_redirect():
    """Redirect /dashboard/setup to Zoom config (current tenant)."""
    return RedirectResponse(url="/dashboard/zoom", status_code=302)


@router.get("/zoom", response_class=HTMLResponse)
async def dashboard_zoom(request: Request, user: dict = Depends(require_user)):
    """Zoom account management — tenant-isolated."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    accounts = db.get_zoom_accounts(tenant_id)
    display_accounts = []
    for a in accounts:
        d = dict(a)
        d["client_id_display"] = a.get("client_id", "")[:8] + "****" if a.get("client_id") else ""
        d["has_client_secret"] = bool(a.get("client_secret"))
        display_accounts.append(d)
    return _render_admin(request, "settings", user, "tenant_zoom.html", accounts=display_accounts)


@router.get("/channels", response_class=HTMLResponse)
async def dashboard_channels(request: Request, user: dict = Depends(require_user)):
    """Push channel management — tenant-isolated."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    channels = [dict(c) for c in db.get_tenant_channels(tenant_id)]
    bot_config = db.get_tenant_bot_config(tenant_id)
    return _render_admin(request, "settings", user, "tenant_channels.html",
                          channels=channels, bot_config=bot_config)


# ── Admin: Tenants ────────────────────────────────────────────────────────────

@router.get("/admin/tenants", response_class=HTMLResponse)
async def admin_tenants(request: Request, user: dict = Depends(require_user)):
    """Tenant management page."""
    all_tenants = db.get_all_tenants()
    tenants = [_tenant_dict(t) for t in all_tenants]
    return _render_admin(request, "admin", user, "admin_tenants.html",
                         tenants=tenants)


@router.post("/admin/tenants/create")
async def admin_tenants_create(request: Request,
                               name: str = Form(...),
                               display_name: str = Form(""),
                               plan: str = Form("pro"),
                               user: dict = Depends(require_user)):
    """Create a new tenant."""
    tenant_id = db.create_tenant(name, display_name, plan)
    request.session["tenant_id"] = tenant_id
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.post("/admin/tenants/{tenant_id}/toggle")
async def admin_tenants_toggle(request: Request, tenant_id: str,
                               user: dict = Depends(require_user)):
    """Toggle tenant active/inactive."""
    db.toggle_tenant(tenant_id)
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.post("/admin/tenants/{tenant_id}/switch")
async def admin_tenants_switch(request: Request, tenant_id: str,
                               user: dict = Depends(require_user)):
    """Switch admin context to this tenant."""
    request.session["tenant_id"] = tenant_id
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.post("/admin/tenants/{tenant_id}/delete")
async def admin_tenants_delete(request: Request, tenant_id: str,
                               user: dict = Depends(require_user)):
    """Delete a tenant."""
    db.delete_tenant(tenant_id)
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.post("/admin/tenants/{tenant_id}/token")
async def admin_tenants_token(request: Request, tenant_id: str,
                              user: dict = Depends(require_user)):
    """Regenerate tenant API token."""
    db.regenerate_tenant_token(tenant_id)
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


# ── Admin: Users ─────────────────────────────────────────────────────────────

@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, user: dict = Depends(require_user)):
    """User management page."""
    all_users = db.get_all_users()
    users = [_user_dict(u) for u in all_users]
    return _render_admin(request, "admin", user, "admin_users.html", users=users)


@router.post("/members/update-display")
async def update_member_display_api(request: Request):
    """更新成员别名/备注/计入统计"""
    try:
        data = await request.json()
        from db import _get_conn
        raw_name = data.get("raw_name", "").strip()
        display_name = data.get("display_name", "").strip()
        note = data.get("note", "")
        count_enabled = data.get("count_enabled", True)
        aliases = data.get("aliases", [])
        if not raw_name or not display_name:
            return {"ok": False, "message": "参数不完整"}
        import json
        conn = _get_conn()
        tenant_id = data.get("tenant_id") or request.app.state.get_effective_tenant_id(request)
        existing = conn.execute(
            "SELECT id FROM member_display WHERE raw_name = ? AND tenant_id = ?", (raw_name, tenant_id)
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE member_display SET display_name=?, aliases=?, note=?, count_enabled=?, updated_at=datetime('now') WHERE raw_name=? AND tenant_id=?",
                (display_name, json.dumps(aliases), note, int(count_enabled), raw_name, tenant_id)
            )
        else:
            conn.execute(
                "INSERT INTO member_display (raw_name, display_name, aliases, note, count_enabled, tenant_id) VALUES (?,?,?,?,?,?)",
                (raw_name, display_name, json.dumps(aliases), note, int(count_enabled), tenant_id)
            )
        conn.commit()
        return {"ok": True, "message": "保存成功"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


@router.post("/admin/users/create")
async def admin_users_create(request: Request,
                             username: str = Form(...),
                             password: str = Form(...),
                             display_name: str = Form(""),
                             role: str = Form("viewer"),
                             user: dict = Depends(require_user)):
    """Create a new user."""
    try:
        uid = db.create_user(username, password, display_name, role)
        # Also add to current tenant
        tenant_id = request.app.state.get_effective_tenant_id(request)
        db.set_user_tenant_role(uid, tenant_id, role)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    return RedirectResponse(url="/dashboard/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/toggle")
async def admin_users_toggle(request: Request, user_id: int,
                             user: dict = Depends(require_user)):
    """Toggle user active/inactive."""
    db.toggle_user(user_id)
    return RedirectResponse(url="/dashboard/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/delete")
async def admin_users_delete(request: Request, user_id: int,
                             user: dict = Depends(require_user)):
    """Delete a user."""
    db.delete_user(user_id)
    return RedirectResponse(url="/dashboard/admin/users", status_code=303)


# ── Admin: Zoom Accounts ─────────────────────────────────────────────────────

@router.get("/admin/accounts", response_class=HTMLResponse)
async def admin_accounts(request: Request, user: dict = Depends(require_user)):
    """Zoom account & meeting management page."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    accounts = [_account_dict(a) for a in db.get_zoom_accounts(tenant_id)]
    meetings = [_meeting_dict(m) for m in db.get_meetings(tenant_id)]
    return _render_admin(request, "admin", user, "admin_accounts.html",
                         accounts=accounts, meetings=meetings, tenants=db.get_all_tenants())


@router.post("/admin/accounts/create")
async def admin_accounts_create(request: Request,
                                label: str = Form(""),
                                account_id: str = Form(...),
                                client_id: str = Form(...),
                                client_secret: str = Form(...),
                                host_email: str = Form(""),
                                webhook_secret: str = Form(""),
                                user: dict = Depends(require_user)):
    """Create a new Zoom account binding."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    db.create_zoom_account(tenant_id, label, account_id, client_id, client_secret, host_email, webhook_secret)
    return RedirectResponse(url="/dashboard/admin/accounts", status_code=303)


@router.post("/admin/accounts/{account_id}/edit")
async def admin_accounts_edit(request: Request, account_id: int,
                              label: str = Form(""),
                              host_email: str = Form(""),
                              webhook_secret: str = Form(""),
                              is_active: str = Form("1"),
                              client_id: str = Form(""),
                              client_secret: str = Form(""),
                              user: dict = Depends(require_user)):
    """Edit a Zoom account."""
    updates = dict(
        label=label,
        host_email=host_email,
        webhook_secret=webhook_secret,
        is_active=int(is_active),
    )
    # Only update credentials if provided
    if client_id:
        updates["client_id"] = client_id
    if client_secret:
        updates["client_secret"] = client_secret
    db.update_zoom_account(account_id, **updates)
    return RedirectResponse(url="/dashboard/admin/accounts", status_code=303)


@router.post("/admin/accounts/{account_id}/delete")
async def admin_accounts_delete(request: Request, account_id: int,
                                user: dict = Depends(require_user)):
    """Delete a Zoom account."""
    db.delete_zoom_account(account_id)
    return RedirectResponse(url="/dashboard/admin/accounts", status_code=303)


# ── Admin: Accounts API ──────────────────────────────────────────────────────

@router.get("/admin/accounts/{account_id}/webhook-status")
async def admin_accounts_webhook_status(request: Request, account_id: int,
                                         user: dict = Depends(require_user)):
    """Get webhook status for a Zoom account."""
    account = db.get_zoom_account(account_id)
    if not account:
        return JSONResponse({"ok": False, "error": "Account not found"})
    return JSONResponse({
        "ok": True,
        "webhook_last_event": account.get("webhook_last_event", ""),
        "webhook_last_time": account.get("webhook_last_time", ""),
        "status": account.get("status", "inactive"),
    })


@router.post("/admin/accounts/{account_id}/test")
async def admin_accounts_test(request: Request, account_id: int,
                               user: dict = Depends(require_user)):
    """Test Zoom API connection for a specific account."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    account = db.get_zoom_account(account_id)
    if not account:
        return JSONResponse({"ok": False, "error": "Account not found"})

    db.update_zoom_account_status(account_id, "verifying")

    api = ZoomAPI(
        account_id=account["account_id"],
        client_id=account["client_id"],
        client_secret=account["client_secret"],
        tenant_id=account["tenant_id"],
    )
    try:
        result = await api.test_connection()
        if result["ok"]:
            db.update_zoom_account_status(
                account_id, "active",
                last_sync=now,
                last_sync_result=f"Connected as {result['user'].get('email','?')}",
            )
            # ── 接入测试后自动检测能力 ──
            try:
                caps = await api.detect_capabilities()
                db.update_tenant_capabilities(account["tenant_id"], caps)
                result["capabilities"] = caps
            except Exception as e:
                result["capability_error"] = str(e)
        else:
            db.update_zoom_account_status(
                account_id, "error",
                last_sync=now,
                last_sync_result=result.get("error", "Unknown error"),
            )
        return JSONResponse({"ok": True, "result": result})
    except Exception as e:
        db.update_zoom_account_status(account_id, "error", last_sync=now, last_sync_result=str(e))
        return JSONResponse({"ok": True, "result": {"ok": False, "error": str(e)}})


# ── Admin: Meetings ──────────────────────────────────────────────────────────

@router.post("/admin/meetings/create")
async def admin_meetings_create(request: Request,
                                meeting_id: str = Form(...),
                                label: str = Form(""),
                                meeting_type: str = Form("pmi"),
                                user: dict = Depends(require_user)):
    """Add a monitored meeting room."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    db.create_meeting(tenant_id, 0, meeting_id, label, meeting_type)
    return RedirectResponse(url="/dashboard/admin/accounts", status_code=303)


@router.post("/admin/meetings/{meeting_db_id}/delete")
async def admin_meetings_delete(request: Request, meeting_db_id: int,
                                user: dict = Depends(require_user)):
    """Remove a monitored meeting room."""
    db.delete_meeting(meeting_db_id)
    return RedirectResponse(url="/dashboard/admin/accounts", status_code=303)


# ── Admin: Channels ──────────────────────────────────────────────────────────

@router.get("/admin/channels", response_class=HTMLResponse)
async def admin_channels(request: Request, user: dict = Depends(require_user)):
    """Telegram channel management page."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    channels = [_channel_dict(c) for c in db.get_tenant_channels(tenant_id)]
    return _render_admin(request, "admin", user, "admin_channels.html",
                         channels=channels)


@router.post("/admin/channels/create")
async def admin_channels_create(request: Request,
                                chat_id: str = Form(...),
                                label: str = Form(""),
                                is_group: str = Form("false"),
                                user: dict = Depends(require_user)):
    """Create a new telegram channel."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    db.create_tenant_channel(tenant_id, chat_id, label, is_group == "true")
    return RedirectResponse(url="/dashboard/admin/channels", status_code=303)


@router.post("/admin/channels/{channel_id}/toggle")
async def admin_channels_toggle(request: Request, channel_id: int,
                                user: dict = Depends(require_user)):
    """Toggle channel enabled/disabled."""
    db.toggle_tenant_channel(channel_id)
    return RedirectResponse(url="/dashboard/admin/channels", status_code=303)


@router.post("/admin/channels/{channel_id}/delete")
async def admin_channels_delete(request: Request, channel_id: int,
                                user: dict = Depends(require_user)):
    """Delete a channel."""
    db.delete_tenant_channel(channel_id)
    return RedirectResponse(url="/dashboard/admin/channels", status_code=303)


@router.post("/admin/channels/{channel_id}/edit")
async def admin_channels_edit(request: Request, channel_id: int,
                              label: str = Form(""),
                              chat_id: str = Form(""),
                              is_group: str = Form("false"),
                              user: dict = Depends(require_user)):
    """Edit a channel's label, chat_id, and/or is_group."""
    db.update_tenant_channel(channel_id, label=label.strip(), chat_id=chat_id.strip(),
                             is_group=(is_group == "true"))
    return RedirectResponse(url="/dashboard/admin/channels", status_code=303)


@router.post("/admin/channels/{channel_id}/test")
async def admin_channels_test(request: Request, channel_id: int,
                              user: dict = Depends(require_user)):
    """Send a test push to the given channel."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    channels = db.get_tenant_channels(tenant_id)
    target = next((c for c in channels if c["id"] == channel_id), None)
    if not target:
        return JSONResponse({"ok": False, "error": "Channel not found"}, status_code=404)
    bot_config = db.get_tenant_bot_config(tenant_id)
    token = bot_config["token"] or settings.telegram_bot_token
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(url, json={
                "chat_id": target["chat_id"],
                "text": "✅ 测试消息 — 推送配置正常，机器人已接入",
            })
            data = resp.json()
            return JSONResponse({"ok": data.get("ok", False), "chat_id": target["chat_id"]})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


# ── Rendering helper ──────────────────────────────────────────────────────────

def _render_admin(request: Request, active: str, user: dict, template_name: str,
                  **extra) -> HTMLResponse:
    """Render admin template with common context injected."""
    from fastapi.templating import Jinja2Templates
    from pathlib import Path
    # Use the same templates directory as the main app
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    from app import fmt_myt
    templates.env.globals["fmt_myt"] = fmt_myt

    # Build current_user dict matching template expectations
    tenant_id = request.app.state.get_effective_tenant_id(request)
    current_user = {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name", ""),
        "role": user.get("role", "viewer"),
        "tenant_id": tenant_id,
        "is_active": user["is_active_str"],
    }

    # ── 租户切换上下文（仅 super_admin 需要） ──
    from db import get_all_tenants
    is_super_admin = user.get("role") == "super_admin"
    if is_super_admin:
        all_tenants = get_all_tenants()
        current_tenant = next((t for t in all_tenants if t["id"] == tenant_id), None)
        current_tenant_name = current_tenant["display_name"] if current_tenant else tenant_id
    else:
        all_tenants = []
        current_tenant_name = ""

    context = {
        "request": request,
        "active": active,
        "current_user": current_user,
        "page_title": extra.pop("title", "成员中心"),
        "is_super_admin": is_super_admin,
        "available_tenants": all_tenants,
        "current_tenant_id": tenant_id,
        "current_tenant_name": current_tenant_name,
        **extra,
    }
    return templates.TemplateResponse(request, template_name, context)
