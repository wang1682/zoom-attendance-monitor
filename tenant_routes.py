"""Tenant self-service dashboard routes for Zoom Attendance Monitor.
Mounted as an APIRouter under /dashboard/tenant in the main app.
Tenants see only their own data and can manage channels + alert rules."""

import httpx
from datetime import datetime, timedelta, timezone
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


async def require_editor(user: dict = Depends(require_user)) -> dict:
    """Require at least tenant role (viewer cannot modify)."""
    if user.get("role") == "viewer":
        raise HTTPException(status_code=403, detail="Viewer cannot perform this action")
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

    is_viewer = user.get("role", "viewer") == "viewer"
    context = {
        "request": request,
        "active": active,
        "current_user": current_user,
        "tenant_name": tenant_name,
        "is_viewer": is_viewer,
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


# ── Overview (运营面板) ─────────────────────────────────────────────────────


async def _compute_kpi_data(tenant_id: str) -> dict:
    """Compute KPI data for tenant dashboard — all queries tenant-isolated."""
    # 今日参与者
    today_participants = len(db.get_today_participants(limit=10000, tenant_id=tenant_id))

    # 当前在线 + 活跃会议 (from Zoom API)
    current_online = 0
    active_meetings = []
    try:
        accounts = db.get_zoom_accounts(tenant_id)
        active = next(
            (a for a in accounts if a.get("is_active") and a.get("status") == "active"),
            None,
        )
        if active:
            from zoom_metrics import ZoomMetrics
            zm = ZoomMetrics(active)
            live_data = await zm.get_live()
            current_online = live_data.get("total_online", 0)
            meetings_raw = live_data.get("meetings", [])
            active_meetings = [
                {
                    "id": m.get("id", ""),
                    "topic": m.get("topic", ""),
                    "participant_count": len(m.get("participants", [])),
                    "start_time": m.get("start_time", ""),
                }
                for m in meetings_raw
            ]
    except Exception:
        pass

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
    user: dict = Depends(require_editor),
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
    user: dict = Depends(require_editor),
    label: str = Form(""),
    account_id: str = Form(...),
    client_id: str = Form(""),
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
    tenant_id = request.session.get("tenant_id", "default")
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
    tenant_id = request.session.get("tenant_id", "default")
    acct = db.get_zoom_account(account_db_id)
    if not acct or str(acct.get("tenant_id")) != str(tenant_id):
        raise HTTPException(status_code=404)
    db.delete_zoom_account(account_db_id)
    return RedirectResponse(url="/dashboard/tenant/zoom", status_code=303)


# ── Telegram 频道管理 ────────────────────────────────────────────────────────


@router.get("/channels", response_class=HTMLResponse)
async def tenant_channels_page(request: Request, user: dict = Depends(require_user)):
    tenant_id = request.session.get("tenant_id", "default")
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
    tenant_id = request.session.get("tenant_id", "default")
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
    tenant_id = request.session.get("tenant_id", "default")
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
            return JSONResponse({"ok": data.get("ok", False), "chat_id": target["chat_id"]})
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
    tenant_id = request.session.get("tenant_id", "default")
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
    tenant_id = request.session.get("tenant_id", "default")
    db.update_tenant_bot_config(tenant_id, "", "", "")
    return RedirectResponse(url="/dashboard/tenant/channels", status_code=303)


# ── 告警规则开关 ───────────────────────────────────────────────────────────────


@router.get("/alerts", response_class=HTMLResponse)
async def tenant_alerts_page(request: Request, user: dict = Depends(require_user)):
    tenant_id = request.session.get("tenant_id", "default")
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
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
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
    tenant_id = request.session.get("tenant_id", "default")
    return await _compute_setup_status(tenant_id)
