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
        "account_id": a.get("account_id", ""),
        "host_email": a.get("host_email", ""),
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
    """Main dashboard landing page — redirect to admin tenants."""
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.get("/events", response_class=HTMLResponse)
async def dashboard_events(request: Request, user: dict = Depends(require_user)):
    """Events page — redirect to admin tenants for now."""
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.get("/participants", response_class=HTMLResponse)
async def dashboard_participants(request: Request, user: dict = Depends(require_user)):
    """Participants page — redirect to admin tenants for now."""
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.get("/alerts", response_class=HTMLResponse)
async def dashboard_alerts(request: Request, user: dict = Depends(require_user)):
    """Alerts page — redirect to admin tenants for now."""
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.get("/settings", response_class=HTMLResponse)
async def dashboard_settings(request: Request, user: dict = Depends(require_user)):
    """Settings page — redirect to admin for now."""
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


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
        tenant_id = request.session.get("tenant_id", "default")
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
    tenant_id = request.session.get("tenant_id", "default")
    accounts = [_account_dict(a) for a in db.get_zoom_accounts(tenant_id)]
    meetings = [_meeting_dict(m) for m in db.get_meetings(tenant_id)]
    return _render_admin(request, "admin", user, "admin_accounts.html",
                         accounts=accounts, meetings=meetings)


@router.post("/admin/accounts/create")
async def admin_accounts_create(request: Request,
                                label: str = Form(""),
                                account_id: str = Form(...),
                                client_id: str = Form(...),
                                client_secret: str = Form(...),
                                host_email: str = Form(""),
                                user: dict = Depends(require_user)):
    """Create a new Zoom account binding."""
    tenant_id = request.session.get("tenant_id", "default")
    db.create_zoom_account(tenant_id, label, account_id, client_id, client_secret, host_email)
    return RedirectResponse(url="/dashboard/admin/accounts", status_code=303)


@router.post("/admin/accounts/{account_id}/delete")
async def admin_accounts_delete(request: Request, account_id: int,
                                user: dict = Depends(require_user)):
    """Delete a Zoom account."""
    db.delete_zoom_account(account_id)
    return RedirectResponse(url="/dashboard/admin/accounts", status_code=303)


# ── Admin: Meetings ──────────────────────────────────────────────────────────

@router.post("/admin/meetings/create")
async def admin_meetings_create(request: Request,
                                meeting_id: str = Form(...),
                                label: str = Form(""),
                                meeting_type: str = Form("pmi"),
                                user: dict = Depends(require_user)):
    """Add a monitored meeting room."""
    tenant_id = request.session.get("tenant_id", "default")
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
    tenant_id = request.session.get("tenant_id", "default")
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
    tenant_id = request.session.get("tenant_id", "default")
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


# ── Rendering helper ──────────────────────────────────────────────────────────

def _render_admin(request: Request, active: str, user: dict, template_name: str,
                  **extra) -> HTMLResponse:
    """Render admin template with common context injected."""
    from fastapi.templating import Jinja2Templates
    from pathlib import Path
    # Use the same templates directory as the main app
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

    # Build current_user dict matching template expectations
    tenant_id = request.session.get("tenant_id", "default")
    current_user = {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name", ""),
        "role": user.get("role", "viewer"),
        "tenant_id": tenant_id,
        "is_active": user["is_active_str"],
    }

    context = {
        "request": request,
        "active": active,
        "current_user": current_user,
        **extra,
    }
    return templates.TemplateResponse(request, template_name, context)
