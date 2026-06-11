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
    """Tenant dashboard overview — Setup Center with readiness score."""
    tenant_id = request.session.get("tenant_id", "default")
    status_data = _compute_setup_status(tenant_id)
    return _render_tenant(
        request, "overview", user, "tenant_overview.html",
        score=status_data["score"],
        status_label=status_data["status"],
        checks=status_data["checks"],
        next_steps=status_data["next_steps"],
    )


# ── Zoom 账号自助配置 ──────────────────────────────────────────────────────


@router.get("/zoom", response_class=HTMLResponse)
async def tenant_zoom(request: Request, user: dict = Depends(require_user)):
    """Zoom account self-service page — create, edit, test connection."""
    tenant_id = request.session.get("tenant_id", "default")
    accounts = db.get_zoom_accounts(tenant_id)
    # Mask secrets in display dicts
    display_accounts = []
    for a in accounts:
        d = _account_dict(a)
        d["client_id_display"] = a.get("client_id", "")[:8] + "****" if a.get("client_id") else ""
        d["has_client_secret"] = bool(a.get("client_secret"))
        display_accounts.append(d)
    return _render_tenant(
        request, "zoom", user, "tenant_zoom.html",
        accounts=display_accounts,
    )


@router.post("/zoom/create", response_class=RedirectResponse)
async def tenant_zoom_create(
    request: Request,
    user: dict = Depends(require_user),
    label: str = Form(""),
    account_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
    webhook_secret: str = Form(""),
):
    tenant_id = request.session.get("tenant_id", "default")
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
    user: dict = Depends(require_user),
    label: str = Form(""),
    account_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(""),
    webhook_secret: str = Form(""),
):
    tenant_id = request.session.get("tenant_id", "default")
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


@router.post("/zoom/test")
async def tenant_zoom_test(
    request: Request,
    user: dict = Depends(require_user),
    account_id: str = Form(...),
    client_id: str = Form(...),
    client_secret: str = Form(...),
):
    """Test Zoom OAuth connection with provided credentials."""
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            # Step 1: Get access token
            token_url = "https://zoom.us/oauth/token"
            payload = {
                "grant_type": "account_credentials",
                "account_id": account_id,
            }
            auth = httpx.BasicAuth(client_id, client_secret)
            tr = await cl.post(token_url, data=payload, auth=auth)
            if tr.status_code != 200:
                detail = tr.text[:200]
                return JSONResponse(
                    status_code=200,
                    content={"success": False, "message": f"OAuth 失败 (HTTP {tr.status_code}): {detail}"},
                )
            token_data = tr.json()
            access_token = token_data.get("access_token", "")

            # Step 2: Verify with /users/me
            ur = await cl.get(
                "https://api.zoom.us/v2/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if ur.status_code != 200:
                return JSONResponse(
                    status_code=200,
                    content={"success": False, "message": f"用户验证失败 (HTTP {ur.status_code})"},
                )
            user_data = ur.json()
            host_email = user_data.get("email", "未知")
            host_name = user_data.get("first_name", "") + " " + user_data.get("last_name", "")
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": f"连接成功！主机: {host_name.strip()} ({host_email})",
                    "host_email": host_email,
                },
            )
    except httpx.TimeoutException:
        return JSONResponse(
            status_code=200,
            content={"success": False, "message": "连接超时，请检查 Zoom 服务状态"},
        )
    except Exception as e:
        return JSONResponse(
            status_code=200,
            content={"success": False, "message": f"测试失败: {type(e).__name__}: {str(e)[:200]}"},
        )


@router.post("/zoom/{account_db_id}/test")
async def tenant_zoom_test_existing(
    request: Request,
    account_db_id: int,
    user: dict = Depends(require_user),
):
    """Test connection for an existing account using DB-stored credentials."""
    tenant_id = request.session.get("tenant_id", "default")
    acct = db.get_zoom_account(account_db_id)
    if not acct or str(acct.get("tenant_id")) != str(tenant_id):
        return JSONResponse(status_code=200, content={"success": False, "message": "账号不存在或无权访问"})
    if not acct.get("client_id") or not acct.get("client_secret") or not acct.get("account_id"):
        return JSONResponse(status_code=200, content={"success": False, "message": "账号凭据不完整，请重新编辑"})
    try:
        async with httpx.AsyncClient(timeout=15) as cl:
            token_url = "https://zoom.us/oauth/token"
            payload = {"grant_type": "account_credentials", "account_id": acct["account_id"]}
            auth = httpx.BasicAuth(acct["client_id"], acct["client_secret"])
            tr = await cl.post(token_url, data=payload, auth=auth)
            if tr.status_code != 200:
                return JSONResponse(
                    status_code=200,
                    content={"success": False, "message": f"OAuth 失败 (HTTP {tr.status_code})"},
                )
            token_data = tr.json()
            access_token = token_data.get("access_token", "")
            ur = await cl.get(
                "https://api.zoom.us/v2/users/me",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            if ur.status_code != 200:
                return JSONResponse(
                    status_code=200,
                    content={"success": False, "message": f"用户验证失败 (HTTP {ur.status_code})"},
                )
            user_data = ur.json()
            host_email = user_data.get("email", "未知")
            host_name = f"{user_data.get('first_name', '')} {user_data.get('last_name', '')}".strip()
            return JSONResponse(
                status_code=200,
                content={
                    "success": True,
                    "message": f"连接成功！主机: {host_name} ({host_email})",
                    "host_email": host_email,
                },
            )
    except httpx.TimeoutException:
        return JSONResponse(status_code=200, content={"success": False, "message": "连接超时"})
    except Exception as e:
        return JSONResponse(status_code=200, content={"success": False, "message": f"测试失败: {str(e)[:200]}"})


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


# ── Setup Status API ──────────────────────────────────────────────────────────


def _compute_setup_status(tenant_id: str) -> dict:
    """Compute setup readiness score and checks for a tenant.

    Sync implementation — safe to call from API and SSR routes.
    """
    checks = {}

    # 1. Zoom account configured (20 pts)
    accounts = db.get_zoom_accounts(tenant_id)
    has_account = any(a.get("is_active") and a.get("client_id") for a in accounts)
    checks["zoom_account"] = bool(has_account)

    # 2. OAuth verified (15 pts) — account status == 'active'
    has_oauth = any(
        a.get("is_active") and a.get("status") == "active"
        for a in accounts
    )
    checks["oauth"] = bool(has_oauth)

    # 3. Meetings data (10 pts) — monitored_meetings has entries
    meetings = db.get_meetings(tenant_id) if hasattr(db, 'get_meetings') else []
    checks["meetings"] = len(meetings) > 0

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
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM zoom_events WHERE created_at >= ?",
        (cutoff,),
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
    tenant_id = request.session.get("tenant_id", "default")
    return _compute_setup_status(tenant_id)
