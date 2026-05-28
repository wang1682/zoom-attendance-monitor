"""
phase2/dashboard.py — FastAPI Dashboard 路由（安全版）

所有 /dashboard/* 和 /login /logout 路由。
Phase 8: 所有查询按 tenant_id 隔离。
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi import APIRouter, Request, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import jinja2

from phase2.db import SyncSession
from phase2.models import ZoomEvent, ZoomParticipant, AlertLog
from phase2.auth import (
    handle_login,
    handle_logout,
    get_current_user,
    require_auth,
)
from phase2.models import ROLE_HIERARCHY, ROLE_NAV, ROLE_SCOPES, build_nav_items


router = APIRouter()

TEMPLATE_DIR = str(Path(__file__).parent.parent / "templates")
static_dir = Path(__file__).parent.parent / "static"
static_dir.mkdir(exist_ok=True)

_jinja_env = jinja2.Environment(
    loader=jinja2.FileSystemLoader(TEMPLATE_DIR),
    autoescape=True,
)
_jinja_env.filters['from_json'] = lambda s: json.loads(s) if s else {}


def _get_role(username: str, tenant_id: str = "default") -> str:
    """查询用户角色，.env admin 视为 owner"""
    from app.settings import settings
    if username == settings.dashboard_admin_user:
        return "owner"
    with SyncSession() as s:
        from phase2.models import DashboardUser
        user = s.query(DashboardUser).filter_by(
            username=username, tenant_id=tenant_id
        ).first()
        if user:
            return user.role or "viewer"
    return "viewer"


def _render(name: str, **ctx) -> HTMLResponse:
    """与模板渲染，自动注入 role/nav/scopes/is_admin"""
    request = ctx.get("request")
    user = ctx.get("current_user") or (get_current_user(request) if request else None)
    if user and isinstance(user, dict):
        username = user.get("user", "")
        tenant_id = user.get("tenant_id", "default")
        role = _get_role(username, tenant_id)
        user["role"] = role
        active = ctx.get("active", "")
        ctx["nav_items"] = build_nav_items(role, active)
        ctx["can_manage"] = bool(ROLE_SCOPES.get(role))
    else:
        ctx["role"] = "viewer"
        ctx["nav_items"] = build_nav_items("viewer", "")
        ctx["can_manage"] = False
    # error 页面自动补全默认值
    if name == "error.html":
        ctx.setdefault("status_code", 403)
        ctx.setdefault("title", "访问被拒绝")
    template = _jinja_env.get_template(name)
    html = template.render(**ctx)
    return HTMLResponse(html)


def _login_redirect(request: Request, path: str = "/login") -> RedirectResponse:
    return RedirectResponse(url=path, status_code=302)


def _get_tenant_id(user: dict | None) -> str:
    if user and isinstance(user, dict):
        return user.get("tenant_id", "default")
    return "default"


def _today_start_end() -> tuple:
    from phase2.api import _today_start_end
    return _today_start_end()


def _fetch_participants(
    tenant_id: str = "default",
    action: str = "",
    source: str = "",
    limit: int = 200,
):
    start, end = _today_start_end()
    with SyncSession() as s:
        q = s.query(ZoomParticipant).filter(
            ZoomParticipant.tenant_id == tenant_id,
            ZoomParticipant.action_time >= start,
            ZoomParticipant.action_time <= end,
        )
        if action:
            q = q.filter(ZoomParticipant.action == action)
        if source:
            q = q.filter(ZoomParticipant.source == source)
        rows = q.order_by(ZoomParticipant.action_time.desc()).limit(limit).all()
    return rows


def _fetch_events(tenant_id: str = "default", event_type: str = "", limit: int = 200):
    start, end = _today_start_end()
    with SyncSession() as s:
        q = s.query(ZoomEvent).filter(
            ZoomEvent.tenant_id == tenant_id,
            ZoomEvent.received_at >= start,
            ZoomEvent.received_at <= end,
        )
        if event_type:
            q = q.filter(ZoomEvent.event_type == event_type)
        rows = q.order_by(ZoomEvent.received_at.desc()).limit(limit).all()
    return rows


def _fetch_alerts(
    tenant_id: str = "default",
    message_type: str = "",
    status: str = "",
    limit: int = 200,
):
    start, end = _today_start_end()
    with SyncSession() as s:
        q = s.query(AlertLog).filter(
            AlertLog.tenant_id == tenant_id,
            AlertLog.sent_at >= start,
            AlertLog.sent_at <= end,
        )
        if message_type:
            q = q.filter(AlertLog.message_type == message_type)
        if status == "success":
            q = q.filter(AlertLog.success == True)
        elif status == "failed":
            q = q.filter(AlertLog.success == False)
        rows = q.order_by(AlertLog.sent_at.desc()).limit(limit).all()
    return rows


def _get_event_types(tenant_id: str = "default"):
    start, end = _today_start_end()
    with SyncSession() as s:
        rows = s.query(ZoomEvent.event_type).filter(
            ZoomEvent.tenant_id == tenant_id,
            ZoomEvent.received_at >= start,
            ZoomEvent.received_at <= end,
        ).distinct().all()
    return sorted(set(r[0] for r in rows))


def _get_alert_types(tenant_id: str = "default"):
    start, end = _today_start_end()
    with SyncSession() as s:
        rows = s.query(AlertLog.message_type).filter(
            AlertLog.tenant_id == tenant_id,
            AlertLog.sent_at >= start,
            AlertLog.sent_at <= end,
        ).distinct().all()
    return sorted(set(r[0] for r in rows))


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
            d[col.name] = val
        result.append(d)
    return result


# ─── 公开路由 ────────────────────────────────

@router.get("/", response_class=HTMLResponse)
async def root_redirect(request: Request):
    return _login_redirect(request, "/dashboard")


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request, error: str = ""):
    if require_auth(request):
        return _login_redirect(request, "/dashboard")
    return _render("login.html", request=request, error=error)


@router.post("/login")
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
):
    """P8 多租户登录：优先查 dashboard_users 表，回退 .env admin"""
    from app.settings import settings
    from phase2.audit import log_audit
    from phase2.auth import _login_by_db_user, _login_by_env, set_session_cookie

    # 1. 先查 DB 用户（多租户）
    db_user, msg = _login_by_db_user(username, password)
    if db_user:
        resp = RedirectResponse(url="/dashboard", status_code=302)
        set_session_cookie(resp, db_user.username, tenant_id=db_user.tenant_id)
        log_audit("login_success", username=db_user.username, request=request,
                  tenant_id=db_user.tenant_id)
        return resp

    # 2. 回退 .env admin
    env_tenant, msg = _login_by_env(username, password)
    if env_tenant:
        resp = RedirectResponse(url="/dashboard", status_code=302)
        set_session_cookie(resp, username, tenant_id=env_tenant)
        log_audit("login_success", username=username, request=request,
                  tenant_id=env_tenant)
        return resp

    # 3. 失败
    log_audit("login_failed", username=username, request=request, detail=msg)
    return _render("login.html", request=request, error=msg)


@router.get("/logout")
async def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=302)
    handle_logout(request, resp)
    return resp


# ─── Dashboard 页面（需要登录） ──────────────

def _dashboard_check(request: Request) -> dict | None:
    user = require_auth(request)
    if not user:
        return None
    return user


@router.get("/dashboard", response_class=HTMLResponse)
async def dashboard_index(request: Request):
    user = _dashboard_check(request)
    if user is None:
        return _login_redirect(request)

    tenant_id = _get_tenant_id(user)

    from phase2.api import summary_today, v2_health
    from sqlalchemy import text

    s = await summary_today()
    h = await v2_health()

    participants = _fetch_participants(tenant_id=tenant_id, limit=5)
    alerts = _fetch_alerts(tenant_id=tenant_id, limit=5)

    tables = {}
    with SyncSession() as session:
        for tbl in ("zoom_events", "zoom_participants", "alert_logs",
                     "telegram_channels", "zoom_meetings", "zoom_accounts", "alert_rules"):
            cnt = session.execute(text(f"SELECT COUNT(*) FROM {tbl}")).scalar()
            tables[tbl] = cnt

    return _render("dashboard.html",
        request=request, data=s,
        health={"telegram": h.get("telegram", False)},
        recent_participants=_serialize(participants),
        recent_alerts=_serialize(alerts),
        tables=tables,
        current_user=user,
    )


@router.get("/dashboard/events", response_class=HTMLResponse)
async def dashboard_events(request: Request, type: str = ""):
    user = _dashboard_check(request)
    if user is None:
        return _login_redirect(request)
    tenant_id = _get_tenant_id(user)
    events = _fetch_events(tenant_id=tenant_id, event_type=type)
    event_types = _get_event_types(tenant_id=tenant_id)
    return _render("events.html",
        request=request, events=_serialize(events),
        event_types=event_types, filter_type=type,
        current_user=user,
    )


@router.get("/dashboard/participants", response_class=HTMLResponse)
async def dashboard_participants(
    request: Request,
    action: str = "",
    source: str = "",
):
    user = _dashboard_check(request)
    if user is None:
        return _login_redirect(request)
    tenant_id = _get_tenant_id(user)
    participants = _fetch_participants(tenant_id=tenant_id, action=action, source=source)
    return _render("participants.html",
        request=request, participants=_serialize(participants),
        filter_action=action, filter_source=source,
        current_user=user,
    )


@router.get("/dashboard/alerts", response_class=HTMLResponse)
async def dashboard_alerts(
    request: Request,
    type: str = "",
    status: str = "",
):
    user = _dashboard_check(request)
    if user is None:
        return _login_redirect(request)
    tenant_id = _get_tenant_id(user)
    alerts = _fetch_alerts(tenant_id=tenant_id, message_type=type, status=status)
    alert_types = _get_alert_types(tenant_id=tenant_id)
    return _render("alerts.html",
        request=request, alerts=_serialize(alerts),
        alert_types=alert_types, filter_type=type, filter_status=status,
        current_user=user,
    )


@router.get("/dashboard/summary", response_class=HTMLResponse)
async def dashboard_summary(request: Request):
    user = _dashboard_check(request)
    if user is None:
        return _login_redirect(request)
    from phase2.api import summary_today
    s = await summary_today()
    return _render("summary.html",
        request=request, data=s,
        data_json=json.dumps(s, indent=2, ensure_ascii=False),
        current_user=user,
    )


# ─── Phase 5: 配置中心页面 ────────────

@router.get("/dashboard/settings", response_class=HTMLResponse)
async def dashboard_settings(request: Request):
    if _dashboard_check(request) is None:
        return _login_redirect(request)
    from phase2.config_api import get_settings
    data = await get_settings(request, auth_user="dashboard")
    return _render("settings.html", request=request, active="settings", data=data)


@router.get("/dashboard/rules", response_class=HTMLResponse)
async def dashboard_rules(request: Request):
    if _dashboard_check(request) is None:
        return _login_redirect(request)
    from phase2.config_api import get_rules
    rules = await get_rules(request, auth_user="dashboard")
    return _render("rules.html", request=request, active="rules", rules=rules)


@router.get("/dashboard/channels", response_class=HTMLResponse)
async def dashboard_channels(request: Request):
    if _dashboard_check(request) is None:
        return _login_redirect(request)
    from phase2.config_api import get_channels
    channels = await get_channels(request, auth_user="dashboard")
    return _render("channels.html", request=request, active="channels", channels=channels)


# ─── Phase 7: 分析看板 ────────────

@router.get("/dashboard/analytics", response_class=HTMLResponse)
async def dashboard_analytics(request: Request):
    user = _dashboard_check(request)
    if user is None:
        return _login_redirect(request)
    tenant_id = _get_tenant_id(user)
    with SyncSession() as s:
        from app.analytics.models import RiskScore, AiReport
        from app.models import DailyStat
        daily = s.query(DailyStat).filter(
            DailyStat.tenant_id == tenant_id
        ).order_by(DailyStat.date.desc()).limit(14).all()
        risks = s.query(RiskScore).filter(
            RiskScore.tenant_id == tenant_id
        ).order_by(RiskScore.created_at.desc()).limit(10).all()
        reports = s.query(AiReport).filter(
            AiReport.tenant_id == tenant_id
        ).order_by(AiReport.created_at.desc()).limit(5).all()
    return _render("analytics.html",
        request=request, active="analytics",
        daily=daily, risks=risks, reports=reports,
        _serialize=_serialize,
        current_user=user,
    )


@router.get("/dashboard/analytics/daily", response_class=HTMLResponse)
async def dashboard_analytics_daily(request: Request, date: str = ""):
    user = _dashboard_check(request)
    if user is None:
        return _login_redirect(request)
    tenant_id = _get_tenant_id(user)
    with SyncSession() as s:
        from app.models import DailyStat, HourlyActivity
        from app.analytics.models import ParticipantDailyStat
        q = s.query(DailyStat).filter(
            DailyStat.tenant_id == tenant_id
        ).order_by(DailyStat.date.desc()).limit(30)
        days = q.all()
        target_date = date or (days[0].date if days else "")
        qh = s.query(HourlyActivity).filter(
            HourlyActivity.tenant_id == tenant_id,
            HourlyActivity.date == target_date,
        )
        hours = qh.order_by(HourlyActivity.hour).all()
        qp = s.query(ParticipantDailyStat).filter(
            ParticipantDailyStat.tenant_id == tenant_id,
            ParticipantDailyStat.date == target_date,
        )
        participants = qp.order_by(ParticipantDailyStat.total_duration_minutes.desc()).all()
    return _render("analytics_daily.html",
        request=request, active="analytics",
        days=days, hours=hours, participants=participants,
        selected_date=target_date,
        _serialize=_serialize,
        current_user=user,
    )


@router.get("/dashboard/analytics/risks", response_class=HTMLResponse)
async def dashboard_analytics_risks(request: Request, risk_type: str = ""):
    user = _dashboard_check(request)
    if user is None:
        return _login_redirect(request)
    tenant_id = _get_tenant_id(user)
    with SyncSession() as s:
        from app.analytics.models import RiskScore
        q = s.query(RiskScore).filter(RiskScore.tenant_id == tenant_id)
        if risk_type:
            q = q.filter(RiskScore.risk_type == risk_type)
        risks = q.order_by(RiskScore.created_at.desc()).limit(100).all()
        types = [r[0] for r in s.query(RiskScore.risk_type).filter(
            RiskScore.tenant_id == tenant_id
        ).distinct().all()]
    return _render("analytics_risks.html",
        request=request, active="analytics",
        risks=risks, risk_types=types, filter_type=risk_type,
        _serialize=_serialize,
        current_user=user,
    )


@router.get("/dashboard/analytics/reports", response_class=HTMLResponse)
async def dashboard_analytics_reports(request: Request, date: str = ""):
    user = _dashboard_check(request)
    if user is None:
        return _login_redirect(request)
    tenant_id = _get_tenant_id(user)
    with SyncSession() as s:
        from app.analytics.models import AiReport
        q = s.query(AiReport).filter(AiReport.tenant_id == tenant_id)
        if date:
            q = q.filter(AiReport.date == date)
        reports = q.order_by(AiReport.created_at.desc()).limit(30).all()
        dates = [r[0] for r in s.query(AiReport.date).filter(
            AiReport.tenant_id == tenant_id
        ).distinct().order_by(AiReport.date.desc()).limit(30).all()]
    return _render("analytics_reports.html",
        request=request, active="analytics",
        reports=reports, report_dates=dates, filter_date=date,
        _serialize=_serialize,
        current_user=user,
    )
