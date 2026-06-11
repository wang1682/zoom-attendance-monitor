"""Tenant self-service dashboard routes for Zoom Attendance Monitor.
Mounted as an APIRouter under /dashboard/tenant in the main app.
Tenants see only their own data and can manage channels + alert rules."""

import httpx
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Form, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
from config import settings

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


# ── Render helper ─────────────────────────────────────────────────────────────


def _render_tenant(request: Request, active: str, user: dict,
                   template_name: str, **extra) -> HTMLResponse:
    from pathlib import Path
    from fastapi.templating import Jinja2Templates

    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    tenant_id = request.session.get("tenant_id", "default")

    current_user = {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name", ""),
        "role": user.get("role", "viewer"),
        "tenant_id": tenant_id,
        "is_active": user["is_active_str"],
    }
    tenant_info = db.get_tenant(tenant_id)
    tenant_name = tenant_info.get("display_name", tenant_id) if tenant_info else tenant_id

    context = {
        "request": request,
        "active": active,
        "current_user": current_user,
        "tenant_name": tenant_name,
        **extra,
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


# ── Overview ──────────────────────────────────────────────────────────────────


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def tenant_overview(request: Request, user: dict = Depends(require_user)):
    """Tenant dashboard overview — status summary."""
    tenant_id = request.session.get("tenant_id", "default")

    accounts = db.get_zoom_accounts(tenant_id)
    active_account = next((a for a in accounts if a.get("is_active") and a.get("status") == "active"), None)
    zoom_status = "connected" if active_account else "not_configured"
    zoom_email = active_account.get("host_email", "") if active_account else ""

    channels = db.get_tenant_channels(tenant_id)
    enabled_channels = [c for c in channels if c.get("is_enabled")]
    disabled_channels = [c for c in channels if not c.get("is_enabled")]

    rules = db.get_telegram_rules_by_tenant(tenant_id)
    enabled_rules = [r for r in rules if r["enabled"]]

    return _render_tenant(
        request, "overview", user, "tenant_overview.html",
        zoom_status=zoom_status,
        zoom_email=zoom_email,
        channel_count=len(channels),
        enabled_channel_count=len(enabled_channels),
        disabled_channel_count=len(disabled_channels),
        channels=enabled_channels,
        rule_count=len(rules),
        enabled_rule_count=len(enabled_rules),
        rules=enabled_rules,
    )


# ── Zoom 账号（只读）────────────────────────────────────────────────────────


@router.get("/zoom", response_class=HTMLResponse)
async def tenant_zoom(request: Request, user: dict = Depends(require_user)):
    """Zoom account status page — read-only view."""
    tenant_id = request.session.get("tenant_id", "default")
    accounts = [_account_dict(a) for a in db.get_zoom_accounts(tenant_id)]
    return _render_tenant(request, "zoom", user, "tenant_zoom.html", accounts=accounts)


# ── Telegram 频道管理 ────────────────────────────────────────────────────────


@router.get("/channels", response_class=HTMLResponse)
async def tenant_channels_page(request: Request, user: dict = Depends(require_user)):
    tenant_id = request.session.get("tenant_id", "default")
    channels = [_channel_dict(c) for c in db.get_tenant_channels(tenant_id)]
    return _render_tenant(request, "channels", user, "tenant_channels.html", channels=channels)


@router.post("/channels/create")
async def tenant_channels_create(request: Request,
                                  chat_id: str = Form(...),
                                  label: str = Form(""),
                                  is_group: str = Form("false"),
                                  user: dict = Depends(require_user)):
    tenant_id = request.session.get("tenant_id", "default")
    db.create_tenant_channel(tenant_id, chat_id.strip(), label.strip(), is_group == "true")
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


@router.post("/channels/{channel_id}/toggle")
async def tenant_channels_toggle(request: Request, channel_id: int,
                                  user: dict = Depends(require_user)):
    db.toggle_tenant_channel(channel_id)
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


@router.post("/channels/{channel_id}/delete")
async def tenant_channels_delete(request: Request, channel_id: int,
                                  user: dict = Depends(require_user)):
    db.delete_tenant_channel(channel_id)
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


@router.post("/channels/{channel_id}/test")
async def tenant_channels_test(request: Request, channel_id: int,
                                user: dict = Depends(require_user)):
    """Send a test push to the given channel."""
    tenant_id = request.session.get("tenant_id", "default")
    channels = db.get_tenant_channels(tenant_id)
    target = next((c for c in channels if c["id"] == channel_id), None)
    if not target:
        return JSONResponse({"ok": False, "error": "Channel not found"}, status_code=404)
    # Send a simple Telegram message directly
    token = settings.telegram_bot_token
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


# ── 告警规则开关 ───────────────────────────────────────────────────────────────


@router.get("/alerts", response_class=HTMLResponse)
async def tenant_alerts_page(request: Request, user: dict = Depends(require_user)):
    tenant_id = request.session.get("tenant_id", "default")
    rules = db.get_telegram_rules_by_tenant(tenant_id)
    channels = db.get_tenant_channels(tenant_id)
    return _render_tenant(request, "alerts", user, "tenant_alerts.html",
                          rules=rules, channels=channels)


@router.post("/alerts/toggle")
async def tenant_alerts_toggle(request: Request,
                                event_type: str = Form(...),
                                enabled: int = Form(...),
                                target_chat_id: str = Form(""),
                                user: dict = Depends(require_user)):
    """Toggle a single alert rule on/off and/or rebind channel."""
    db.upsert_telegram_rule(event_type, {
        "enabled": enabled,
        "target_chat_id": target_chat_id or "",
    })
    return RedirectResponse(url="/dashboard/tenant/alerts", status_code=303)
