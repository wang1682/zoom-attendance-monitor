"""
Multi-tenant admin dashboard routes for Zoom Attendance Monitor.
Mounted as an APIRouter under /dashboard in the main app.
"""
import json
import re
import time
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Form, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
from config import settings
from zoom_api import ZoomAPI
from tenant_routes import _get_nav_items

router = APIRouter()

# ── Performance helpers ──
PERF_WARN_MS = 1000

def _t_ms() -> float:
    return time.monotonic() * 1000

def _log_perf(label: str, ms: float) -> None:
    if ms >= PERF_WARN_MS:
        import logging; logging.getLogger("perf").warning(f"[PERF] {label} took {ms:.0f}ms")
    else:
        import logging; logging.getLogger("perf").info(f"[PERF] {label} {ms:.0f}ms")


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


def require_role(role: str):
    """Dependency factory: require minimum role level."""
    async def _check(user: dict = Depends(require_user)) -> dict:
        if db.ROLE_HIERARCHY.get(user.get("role", "user"), 0) < db.ROLE_HIERARCHY.get(role, 0):
            raise HTTPException(status_code=403, detail="权限不足")
        return user
    return _check


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
        "is_active": u.get("is_active", 1),
        "active": "true" if u.get("is_active") else "false",
        "tenant_id": u.get("tenant_id", ""),
        "created_at": u.get("created_at", ""),
        "updated_at": u.get("updated_at", ""),
        "telegram_chat_id": u.get("telegram_chat_id", ""),
        "telegram_2fa_enabled": u.get("telegram_2fa_enabled", 0),
        "telegram_2fa_verified_at": u.get("telegram_2fa_verified_at", ""),
        "last_login": u.get("last_login", ""),
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
    tenant_id = request.app.state.get_effective_tenant_id(request)
    t0 = _t_ms()
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
    score = min(score, 100)
    db_ms = _t_ms() - t0
    _log_perf("dashboard_checks", db_ms)
    # Return skeleton HTML — /dashboard/data fills the rest via JS
    return _render_admin(request, "overview", user, "dashboard.html",
                         score=score, checks=checks,
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

    from db import get_shift_attendance, get_all_groups

    tenant_id = request.app.state.get_effective_tenant_id(request)

    # ── 参数 ──
    search = request.query_params.get("search", "").strip()
    group_filter = request.query_params.get("group", "").strip()
    status_filter = request.query_params.get("status", "").strip()  # "online" or "offline"

    # ── 获取当前在线数据（live source） ──
    live_map = {}
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
                        live_map[name] = p
    except Exception:
        pass

    # ── 数据源标注 ──
    data_source = "metrics" if live_map else "webhook"
    is_realtime = data_source == "metrics"
    metrics_online = bool(live_map)

    # ── 班次出勤分析（替代旧的今日考勤汇总） ──
    summary = get_shift_attendance(tenant_id=tenant_id)
    shift_info = summary.get("shift", {})
    members = summary.get("members", [])

    # ── 合并 live 数据：在线成员状态用实时数据标记 ──
    from db import _fmt_dur
    for m in members:
        sn = m.get("standard_name", "")
        lp = live_map.get(sn)
        if lp:
            m["status"] = "online"
            # 班次在线时长优先用 Metrics 实时数据（取大值）
            live_secs = lp.get("online_minutes", 0) * 60
            shift_secs = m.get("shift_online_minutes", 0) * 60
            actual_secs = max(live_secs, shift_secs)
            m["shift_online_minutes"] = actual_secs // 60
            m["shift_online_duration"] = _fmt_dur(actual_secs)
            # 更新出勤率
            required_secs = shift_info.get("required_minutes", 0) * 60
            if required_secs > 0:
                m["attendance_rate"] = min(actual_secs / required_secs, 1.0)
                m["absent_minutes"] = max(0, (required_secs - actual_secs) // 60)
            jt = lp.get("join_time", "")
            if jt:
                m["last_activity"] = jt
        else:
            m["status"] = "offline"

    # 排序：在线优先 → 班次在线时长降序
    members.sort(key=lambda m: (0 if m["status"] == "online" else 1, -(m.get("shift_online_minutes", 0) or 0)))
    live_online = len(live_map)
    live_offline = sum(1 for m in members if m.get("status") == "offline")

    # ── 搜索 / 筛选 ──
    if search:
        q = search.lower()
        search_members = []
        for m in members:
            sn = m.get("standard_name", "").lower()
            disp = m.get("display_name", "").lower()
            aliases = m.get("aliases", [])
            alias_match = any(q in a.lower().replace(" ", "") for a in aliases) if aliases else False
            if q in sn or q in disp or alias_match:
                search_members.append(m)
        members = search_members
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
    # 同时建两个索引：key=raw_name & key=display_name
    for r in md_rows:
        raw_name = r[0]
        disp_name = r[1]
        aliases = json.loads(r[2]) if r[2] else []
        entry = {
            "display_name": disp_name,
            "aliases": aliases,
            "note": r[3] or "",
            "count_enabled": bool(r[4]),
            "group_id": r[5],
            "raw_name": raw_name,
        }
        member_displays[raw_name] = entry
        member_displays[disp_name] = entry

    # 给每个 member 补充 raw_name 和 group_id（从 member_displays 取）
    for m in members:
        sn = m.get("standard_name", "")
        md_entry = member_displays.get(sn, {})
        m["raw_name"] = md_entry.get("raw_name", sn)
        m["group_id"] = md_entry.get("group_id")

    return _render_admin(request, "participants", user, "participants.html",
                         title="总管理成员中心" if role in ("admin", "super_admin") else "租户成员中心",
                         members=members,
                         total_members=summary.get("total_members", 0),
                         online_count=live_online,
                         offline_count=live_offline,
                         shift=shift_info,
                         date=shift_info.get("shift_date", ""),
                         groups=all_groups,
                         search=search,
                         group_filter=group_filter,
                         status_filter=status_filter,
                         member_displays=json.dumps(member_displays),
                         tenant_id=tenant_id,
                         data_source=data_source,
                         is_realtime=is_realtime,
                         metrics_online=metrics_online)


# ═══════════════════════════════════════════════════════════════
# 班次统计 (Shifts)
# ═══════════════════════════════════════════════════════════════

@router.get("/shifts", response_class=HTMLResponse)
async def dashboard_shifts(request: Request, user: dict = Depends(require_user)):
    """班次统计页面 — 手动登记的班次出勤统计。"""
    role = user.get("role", "")
    tenant_id = request.app.state.get_effective_tenant_id(request)

    # 参数
    shift_date = request.query_params.get("date", "").strip()
    group_id = request.query_params.get("group", "").strip()
    search_q = request.query_params.get("q", "").strip()
    shift_type = request.query_params.get("shift_type", "").strip()

    from datetime import datetime, timezone, timedelta
    import pytz
    mytz = pytz.timezone("Asia/Kuala_Lumpur")
    today_myt = datetime.now(mytz).strftime("%Y-%m-%d")
    now_myt = datetime.now(mytz)

    if not shift_date:
        shift_date = today_myt

    # 当前班次判断
    current_hour = now_myt.hour
    current_shift_type = None
    default_shift_start = None
    default_shift_end = None
    if 7 <= current_hour < 19:
        current_shift_type = "早班"
        default_shift_start = f"{shift_date}T07:00:00"
        default_shift_end = f"{shift_date}T19:00:00"
    else:
        current_shift_type = "夜班"
        if current_hour < 7:
            default_shift_start = f"{shift_date}T19:00:00"
            next_day = (now_myt + timedelta(days=1)).strftime("%Y-%m-%d")
            default_shift_end = f"{next_day}T07:00:00"
        else:
            default_shift_start = f"{shift_date}T19:00:00"
            next_day = (datetime.fromisoformat(shift_date) + timedelta(days=1)).strftime("%Y-%m-%d")
            default_shift_end = f"{next_day}T07:00:00"

    # 加载已登记成员
    from db import get_shift_assignments
    all_assignments = get_shift_assignments(shift_date=shift_date, tenant_id=tenant_id)

    # 筛选
    if shift_type:
        all_assignments = [a for a in all_assignments if a.get("shift_name") == shift_type]
    if search_q:
        q = search_q.lower()
        all_assignments = [a for a in all_assignments if q in a.get("member_name", "").lower()]

    member_names = [a["member_name"] for a in all_assignments]

    # 获取分组数据
    from db import get_all_groups
    groups = get_all_groups(tenant_id)

    # 如果指定了分组，过滤成员
    if group_id:
        try:
            gid = int(group_id)
            from db import _get_conn
            conn = _get_conn()
            grp_members = conn.execute(
                "SELECT DISTINCT display_name FROM member_display "
                "WHERE tenant_id = ? AND group_id = ? AND display_name IS NOT NULL",
                (tenant_id, gid),
            ).fetchall()
            grp_names = set(r[0] for r in grp_members)
            member_names = [n for n in member_names if n in grp_names]
        except (ValueError, Exception):
            pass

    # 计算班次统计
    from db import get_shift_attendance_for_shift
    shift_stats = []
    shift_summary = {}

    if member_names:
        # 取第一个班次的时间（假设同一批次都是相同班次类型）
        first = all_assignments[0]
        ss_myt = first["shift_start"] if first.get("shift_start") else default_shift_start
        se_myt = first.get("shift_end", default_shift_end)

        shift_stats, shift_summary = get_shift_attendance_for_shift(
            tenant_id=tenant_id,
            shift_start_myt_str=ss_myt,
            shift_end_myt_str=se_myt,
            member_names=member_names,
        )

        # 合并班次名称到统计结果
        assignment_map = {a["member_name"]: a for a in all_assignments}
        for s in shift_stats:
            a = assignment_map.get(s["standard_name"], {})
            s["shift_name"] = a.get("shift_name", current_shift_type)

    # 统计省缺值
    if not shift_summary:
        shift_summary = {
            "meeting_not_open_minutes": 0,
            "required_minutes": 0,
        }

    total_count = len(shift_stats)
    online_count = sum(1 for s in shift_stats if s["status"] == "online")
    offline_count = sum(1 for s in shift_stats if s["status"] == "offline")
    absent_count = sum(1 for s in shift_stats if s["status"] == "absent")
    early_count = sum(1 for s in shift_stats if s.get("early_leave_minutes", 0) > 0)
    avg_rate = round(
        sum(s["attendance_rate"] for s in shift_stats) / total_count, 4
    ) if total_count > 0 else 1.0

    return _render_admin(
        request, "shifts", user, "shifts.html",
        active="shifts",
        shift_stats=shift_stats,
        shift_summary=shift_summary,
        all_assignments=all_assignments,
        groups=groups,
        shift_date=shift_date,
        today_myt=today_myt,
        current_shift_type=current_shift_type,
        default_shift_start=default_shift_start,
        default_shift_end=default_shift_end,
        total_count=total_count,
        online_count=online_count,
        offline_count=offline_count,
        absent_count=absent_count,
        early_count=early_count,
        avg_rate=avg_rate,
        shift_type=shift_type,
        group_id=group_id,
        search_q=search_q,
    )


@router.post("/shifts/new", response_class=HTMLResponse)
async def dashboard_shifts_new(request: Request, user: dict = Depends(require_user)):
    """新增单个班次登记。"""
    role = user.get("role", "")
    tenant_id = request.app.state.get_effective_tenant_id(request)

    form = await request.form()
    member_name = form.get("member_name", "").strip()
    shift_name = form.get("shift_name", "").strip()
    shift_date = form.get("shift_date", "").strip()
    shift_start = form.get("shift_start", "").strip()
    shift_end = form.get("shift_end", "").strip()

    if not all([member_name, shift_name, shift_date, shift_start, shift_end]):
        return RedirectResponse(url="/dashboard/shifts?error=参数不完整", status_code=303)

    from db import create_shift_assignment
    rid = create_shift_assignment(
        tenant_id=tenant_id,
        member_name=member_name,
        shift_name=shift_name,
        shift_date=shift_date,
        shift_start=shift_start,
        shift_end=shift_end,
        created_by=user["id"],
    )

    if rid is None:
        return RedirectResponse(url=f"/dashboard/shifts?date={shift_date}&error=该成员当日已登记", status_code=303)
    return RedirectResponse(url=f"/dashboard/shifts?date={shift_date}", status_code=303)


@router.post("/shifts/delete", response_class=HTMLResponse)
async def dashboard_shifts_delete(request: Request, user: dict = Depends(require_user)):
    """删除班次登记。"""
    role = user.get("role", "")
    tenant_id = request.app.state.get_effective_tenant_id(request)

    form = await request.form()
    assignment_id = form.get("assignment_id", "").strip()
    shift_date = form.get("shift_date", "").strip()

    if not assignment_id:
        return RedirectResponse(url="/dashboard/shifts?error=缺少参数", status_code=303)

    from db import delete_shift_assignment
    delete_shift_assignment(int(assignment_id), tenant_id=tenant_id)

    return RedirectResponse(url=f"/dashboard/shifts?date={shift_date}", status_code=303)


@router.post("/shifts/batch", response_class=HTMLResponse)
async def dashboard_shifts_batch(request: Request, user: dict = Depends(require_user)):
    """批量登记班次。"""
    role = user.get("role", "")
    tenant_id = request.app.state.get_effective_tenant_id(request)

    form = await request.form()
    shift_name = form.get("shift_name", "").strip()
    shift_date = form.get("shift_date", "").strip()
    shift_start = form.get("shift_start", "").strip()
    shift_end = form.get("shift_end", "").strip()
    member_names_raw = form.get("member_names", "").strip()

    if not all([member_names_raw, shift_name, shift_date, shift_start, shift_end]):
        return RedirectResponse(url="/dashboard/shifts?error=参数不完整", status_code=303)

    member_names = [m.strip() for m in member_names_raw.split(",") if m.strip()]

    from db import batch_create_shift_assignments
    entries = [
        {
            "tenant_id": tenant_id,
            "member_name": name,
            "shift_name": shift_name,
            "shift_date": shift_date,
            "shift_start": shift_start,
            "shift_end": shift_end,
            "created_by": user["id"],
        }
        for name in member_names
    ]
    success, failed = batch_create_shift_assignments(entries)

    url = f"/dashboard/shifts?date={shift_date}&batch_result={success}+{failed}"
    return RedirectResponse(url=url, status_code=303)


@router.get("/shifts/members", response_class=JSONResponse)
async def dashboard_shifts_members_api(request: Request, user: dict = Depends(require_user)):
    """API: 获取租户成员列表（用于批量登记弹窗的成员选择）。"""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    group_id = request.query_params.get("group_id", "").strip()

    from db import _get_conn
    conn = _get_conn()
    if group_id:
        try:
            gid = int(group_id)
            rows = conn.execute(
                "SELECT DISTINCT display_name, group_id FROM member_display "
                "WHERE tenant_id = ? AND group_id = ? AND display_name IS NOT NULL "
                "ORDER BY display_name",
                (tenant_id, gid),
            ).fetchall()
        except ValueError:
            rows = []
    else:
        rows = conn.execute(
            "SELECT DISTINCT display_name, group_id FROM member_display "
            "WHERE tenant_id = ? AND display_name IS NOT NULL "
            "ORDER BY display_name",
            (tenant_id,),
        ).fetchall()

    members = [dict(r)["display_name"] for r in rows]
    return {"members": members}


@router.get("/shifts/members-by-group", response_class=JSONResponse)
async def dashboard_shifts_members_by_group(request: Request, user: dict = Depends(require_user)):
    """API: 按分组获取成员列表（多选批量登记用）。"""
    tenant_id = request.app.state.get_effective_tenant_id(request)

    from db import _get_conn, get_all_groups
    conn = _get_conn()

    groups = get_all_groups(tenant_id)
    result = []
    for g in groups:
        rows = conn.execute(
            "SELECT DISTINCT display_name FROM member_display "
            "WHERE tenant_id = ? AND group_id = ? AND display_name IS NOT NULL "
            "ORDER BY display_name",
            (tenant_id, g["id"]),
        ).fetchall()
        result.append({
            "group_id": g["id"],
            "group_name": g["name"],
            "members": [r[0] for r in rows],
        })
    # 无分组成员
    ungrouped = conn.execute(
        "SELECT DISTINCT display_name FROM member_display "
        "WHERE tenant_id = ? AND group_id IS NULL AND display_name IS NOT NULL "
        "ORDER BY display_name",
        (tenant_id,),
    ).fetchall()
    if ungrouped:
        result.insert(0, {
            "group_id": 0,
            "group_name": "未分组",
            "members": [r[0] for r in ungrouped],
        })

    return {"groups": result}


@router.get("/meetings", response_class=HTMLResponse)
async def dashboard_meetings(request: Request, user: dict = Depends(require_user)):
    """Meetings center — live meetings from Zoom Metrics API + history + sharing."""
    from db import get_meeting_history, get_sharing_records, get_zoom_accounts
    from zoom_metrics import ZoomMetrics
    from datetime import datetime, timezone, timedelta

    tenant_id = request.app.state.get_effective_tenant_id(request)
    tab = request.query_params.get("tab", "live")

    # ── Sharing time filter ──
    MYT = timezone(timedelta(hours=8))
    range_val = request.query_params.get("range", "today")
    start_param = request.query_params.get("start", "")
    end_param = request.query_params.get("end", "")

    sharing_start_utc = None
    sharing_end_utc = None
    now_myt = datetime.now(timezone.utc).astimezone(MYT)

    if start_param and end_param:
        # 自定义日期范围：MYT 日期 → UTC 起止
        try:
            s_dt = datetime.strptime(start_param, "%Y-%m-%d").replace(tzinfo=MYT)
            e_dt = datetime.strptime(end_param, "%Y-%m-%d").replace(tzinfo=MYT) + timedelta(days=1)
            sharing_start_utc = s_dt.astimezone(timezone.utc).isoformat()
            sharing_end_utc = e_dt.astimezone(timezone.utc).isoformat()
            range_val = "custom"
        except:
            pass
    elif range_val == "today":
        myt_day_start = now_myt.replace(hour=0, minute=0, second=0, microsecond=0)
        myt_day_end = myt_day_start + timedelta(days=1)
        sharing_start_utc = myt_day_start.astimezone(timezone.utc).isoformat()
        sharing_end_utc = myt_day_end.astimezone(timezone.utc).isoformat()
    elif range_val == "yesterday":
        myt_yesterday = now_myt - timedelta(days=1)
        myt_day_start = myt_yesterday.replace(hour=0, minute=0, second=0, microsecond=0)
        myt_day_end = myt_day_start + timedelta(days=1)
        sharing_start_utc = myt_day_start.astimezone(timezone.utc).isoformat()
        sharing_end_utc = myt_day_end.astimezone(timezone.utc).isoformat()
    elif range_val == "7d":
        myt_day_start = (now_myt - timedelta(days=7)).replace(hour=0, minute=0, second=0, microsecond=0)
        myt_day_end = now_myt.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=1)
        myt_day_end = myt_day_end.replace(hour=0, minute=0, second=0, microsecond=0)
        sharing_start_utc = myt_day_start.astimezone(timezone.utc).isoformat()
        sharing_end_utc = myt_day_end.astimezone(timezone.utc).isoformat()
    elif range_val == "30d":
        myt_day_start = (now_myt - timedelta(days=30)).replace(hour=0, minute=0, second=0, microsecond=0)
        myt_day_end = now_myt.replace(hour=23, minute=59, second=59, microsecond=0) + timedelta(days=1)
        myt_day_end = myt_day_end.replace(hour=0, minute=0, second=0, microsecond=0)
        sharing_start_utc = myt_day_start.astimezone(timezone.utc).isoformat()
        sharing_end_utc = myt_day_end.astimezone(timezone.utc).isoformat()

    # ── Live meetings ──
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
        pass

    history, total_meetings = get_meeting_history(tenant_id, limit=100, offset=0)
    sharing_search = request.query_params.get("search", "")
    sharing_group_id = request.query_params.get("group_id", "")
    sharing, sharing_total, sharing_meta = get_sharing_records(
        tenant_id, limit=500,
        start_time=sharing_start_utc,
        end_time=sharing_end_utc,
        search=sharing_search or None,
        group_id=sharing_group_id or None,
    )

    # 查询本租户所有分组及各组共享人数
    conn = db._get_conn()
    all_groups = conn.execute(
        "SELECT id, name FROM member_groups WHERE tenant_id=? ORDER BY id", (tenant_id,)
    ).fetchall()
    groups_stats = []
    for gid_row, gname in all_groups:
        gid = str(gid_row)
        cnt = sum(1 for sr in sharing if str(sr.get("group_id", "")) == gid)
        groups_stats.append({"group_id": gid, "group_name": gname, "count": cnt})
    # 未分组人数
    ungrouped_cnt = sum(1 for sr in sharing if not sr.get("group_id"))
    groups_stats.insert(0, {"group_id": "", "group_name": "全部", "count": len(sharing)})

    # 显示筛选范围文本
    if range_val == "today":
        range_label = "今天"
    elif range_val == "yesterday":
        range_label = "昨天"
    elif range_val == "7d":
        range_label = "最近7天"
    elif range_val == "30d":
        range_label = "最近30天"
    elif range_val == "custom":
        range_label = f"{start_param} ~ {end_param}"
    else:
        range_label = "全部"

    return _render_admin(request, "meetings", user, "meetings.html",
                         title="会议中心",
                         live_meetings=live,
                         history_meetings=history,
                         total_meetings=total_meetings,
                         sharing_records=sharing,
                         sharing_total=sharing_total,
                         sharing_range_label=range_label,
                         sharing_range=range_val,
                         sharing_start=start_param,
                         sharing_end=end_param,
                         sharing_search=sharing_search,
                         sharing_group_id=sharing_group_id,
                         sharing_groups=groups_stats,
                         tab=tab)


@router.get("/overview", response_class=HTMLResponse)
async def dashboard_overview(request: Request, user: dict = Depends(require_user)):
    """多租户总览页（仅 super_admin）"""
    role = request.session.get("role", "")
    if role != "super_admin":
        return RedirectResponse(url="/dashboard", status_code=303)
    return _render_admin(request, "admin", user, "admin_overview.html",
                         title="多租户总览")


@router.get("/alerts", response_class=HTMLResponse)
async def dashboard_alerts_page(request: Request, user: dict = Depends(require_user)):
    """Alert rules page — tenant-isolated, unified under /dashboard/alerts."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    rules = db.get_rules_with_channels(tenant_id)
    channels = db.get_tenant_channels(tenant_id)
    bot_config = db.get_tenant_bot_config(tenant_id)
    bot_username = bot_config.get("username", "")
    return _render_admin(request, "alerts", user, "tenant_alerts.html",
                         rules=rules, channels=channels,
                         bot_config=bot_config, bot_username=bot_username)


@router.get("/settings", response_class=HTMLResponse)
async def dashboard_settings(request: Request, user: dict = Depends(require_user)):
    """Settings page — system configuration (super_admin/admin)."""
    role = user.get("role", "")
    if role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")

    # Read current settings from DB
    all_settings = db.get_all_settings()

    # Zoom OAuth callback and webhook URLs (read-only)
    from config import settings as app_settings
    base_url = getattr(app_settings, "base_url", "") or "https://example.com"
    oauth_callback_url = f"{base_url.rstrip('/')}/api/v3/zoom/oauth/callback"
    webhook_url = f"{base_url.rstrip('/')}/api/v3/zoom/webhook"

    settings_dict = {
        # Site
        "site_name": all_settings.get("site_name", ""),
        "logo_url": all_settings.get("logo_url", ""),
        "default_timezone": all_settings.get("default_timezone", "Asia/Kuala_Lumpur"),
        "default_language": all_settings.get("default_language", "zh-CN"),
        # Security
        "password_min_length": all_settings.get("password_min_length", "8"),
        "password_require_digit": all_settings.get("password_require_digit", "1"),
        "password_require_upper": all_settings.get("password_require_upper", "1"),
        "password_require_special": all_settings.get("password_require_special", "1"),
        "login_max_attempts": all_settings.get("login_max_attempts", "5"),
        "session_ttl_minutes": all_settings.get("session_ttl_minutes", "60"),
        # Zoom
        "oauth_callback_url": oauth_callback_url,
        "webhook_url": webhook_url,
        "default_scopes": all_settings.get("default_scopes", ""),
        # Telegram
        "default_bot_token": all_settings.get("default_bot_token", ""),
        "default_message_template": all_settings.get("default_message_template", ""),
    }

    return _render_admin(request, "admin_center", user, "system_settings.html",
                         **settings_dict)


@router.post("/settings/update")
async def dashboard_settings_update(request: Request, user: dict = Depends(require_user)):
    """Save system settings."""
    role = user.get("role", "")
    if role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")

    form = await request.form()
    # Map form field names to settings keys
    field_map = {
        "site_name": "site_name",
        "logo_url": "logo_url",
        "default_timezone": "default_timezone",
        "default_language": "default_language",
        "password_min_length": "password_min_length",
        "password_require_digit": "password_require_digit",
        "password_require_upper": "password_require_upper",
        "password_require_special": "password_require_special",
        "login_max_attempts": "login_max_attempts",
        "session_ttl_minutes": "session_ttl_minutes",
        "default_scopes": "default_scopes",
        "default_bot_token": "default_bot_token",
        "default_message_template": "default_message_template",
    }
    for field, key in field_map.items():
        val = form.get(field, "").strip()
        db.set_setting(key, val)

    return RedirectResponse(url="/dashboard/settings", status_code=303)


@router.post("/settings/test-telegram")
async def dashboard_settings_test_telegram(request: Request, user: dict = Depends(require_user)):
    """Test Telegram push with current settings."""
    role = user.get("role", "")
    if role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")

    bot_token = db.get_setting("default_bot_token", "")
    if not bot_token:
        return JSONResponse(status_code=400, content={"ok": False, "message": "请先配置默认 Bot Token"})

    try:
        import httpx
        async with httpx.AsyncClient(timeout=10) as cl:
            # Get bot info first
            r = await cl.get(f"https://api.telegram.org/bot{bot_token}/getMe")
            if r.status_code != 200:
                return JSONResponse(status_code=400, content={
                    "ok": False, "message": f"Bot Token 无效: HTTP {r.status_code}"
                })
            bot_info = r.json()
            bot_username = bot_info.get("result", {}).get("username", "unknown")

            # Get the user's chat_id from session
            chat_id = request.session.get("user_chat_id", "")
            if not chat_id:
                # Try to find the user's telegram_chat_id from DB
                fresh_user = db.get_user_by_id(user["id"])
                chat_id = fresh_user.get("telegram_chat_id", "") if fresh_user else ""

            if not chat_id:
                return JSONResponse(status_code=400, content={
                    "ok": False, "message": "当前用户未绑定 Telegram。请先在安全中心绑定 Telegram 后再测试。"
                })

            # Send test message
            msg = (
                "🧪 *Zoom Monitor 测试推送*\n\n"
                "这是一条来自系统设置的测试消息。\n"
                f"发送时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"Bot: @{bot_username}"
            )
            pr = await cl.post(
                f"https://api.telegram.org/bot{bot_token}/sendMessage",
                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            )
            if pr.status_code == 200:
                return JSONResponse(content={
                    "ok": True, "message": f"✅ 测试推送成功！已发送至 @{bot_username}"
                })
            else:
                return JSONResponse(status_code=400, content={
                    "ok": False, "message": f"发送失败: HTTP {pr.status_code}"
                })
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "ok": False, "message": f"测试推送异常: {type(e).__name__}: {str(e)[:200]}"
        })


@router.get("/setup")
async def dashboard_setup_redirect():
    """Redirect /dashboard/setup to Zoom config (current tenant)."""
    return RedirectResponse(url="/dashboard/zoom", status_code=302)


@router.get("/zoom", response_class=HTMLResponse)
async def dashboard_zoom(request: Request, user: dict = Depends(require_user)):
    """Zoom account management — tenant-isolated."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    role = user.get("role", "")
    # tenant 用户也能进入该页面——由 hide_settings 控制只显示 2FA
    accounts = db.get_zoom_accounts(tenant_id)
    display_accounts = []
    for a in accounts:
        d = dict(a)
        d["client_id_display"] = a.get("client_id", "")[:8] + "****" if a.get("client_id") else ""
        d["has_client_secret"] = bool(a.get("client_secret"))
        display_accounts.append(d)
    fresh_user = db.get_user_by_id(user["id"]) or user
    return _render_admin(request, "settings", fresh_user, "tenant_zoom.html", accounts=display_accounts)


@router.get("/channels", response_class=HTMLResponse)
async def dashboard_channels(request: Request, user: dict = Depends(require_user)):
    """Push channel management — tenant-isolated."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    channels = [dict(c) for c in db.get_tenant_channels(tenant_id)]
    bot_config = db.get_tenant_bot_config(tenant_id)
    return _render_admin(request, "channels", user, "tenant_channels.html",
                          channels=channels, bot_config=bot_config)


# ── Admin Center ─────────────────────────────────────────────────────────────

@router.get("/admin-center", response_class=HTMLResponse)
async def dashboard_admin_center(request: Request, user: dict = Depends(require_user)):
    """Admin center — hub page for management features."""
    role = user.get("role", "")
    if role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")

    # Compute stats
    stats = {
        "total_tenants": db.count_total_tenants(),
        "total_users": db.count_total_users(),
        "total_zoom_accounts": db.count_total_zoom_accounts(),
        "total_channels": db.count_total_channels(),
        "today_alerts": db.count_today_alerts(),
        "today_push_count": db.count_today_push_count(),
    }

    return _render_admin(request, "admin_center", user, "admin_center.html",
                         stats=stats)


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
                               user: dict = Depends(require_role("super_admin"))):
    """Create a new tenant."""
    tenant_id = db.create_tenant(name, display_name, plan)
    request.session["tenant_id"] = tenant_id
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.post("/admin/tenants/{tenant_id}/toggle")
async def admin_tenants_toggle(request: Request, tenant_id: str,
                               user: dict = Depends(require_role("super_admin"))):
    """Toggle tenant active/inactive."""
    db.toggle_tenant(tenant_id)
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.post("/admin/tenants/{tenant_id}/switch")
async def admin_tenants_switch(request: Request, tenant_id: str,
                               user: dict = Depends(require_role("super_admin"))):
    """Switch admin context to this tenant."""
    request.session["tenant_id"] = tenant_id
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.post("/admin/tenants/{tenant_id}/delete")
async def admin_tenants_delete(request: Request, tenant_id: str,
                               user: dict = Depends(require_role("super_admin"))):
    """Delete a tenant."""
    db.delete_tenant(tenant_id)
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


@router.post("/admin/tenants/{tenant_id}/token")
async def admin_tenants_token(request: Request, tenant_id: str,
                              user: dict = Depends(require_role("super_admin"))):
    """Regenerate tenant API token."""
    db.regenerate_tenant_token(tenant_id)
    return RedirectResponse(url="/dashboard/admin/tenants", status_code=303)


# ── Admin: Users ─────────────────────────────────────────────────────────────

@router.get("/admin/users", response_class=HTMLResponse)
async def admin_users(request: Request, user: dict = Depends(require_user)):
    """Redirect to new user management page."""
    return RedirectResponse(url="/dashboard/users", status_code=302)


@router.post("/members/update-display")
async def update_member_display_api(request: Request, user: dict = Depends(require_role("tenant_admin"))):
    """更新成员别名/备注/计入统计"""
    try:
        data = await request.json()
        from db import _get_conn
        raw_name = data.get("raw_name", "").strip()
        display_name = data.get("display_name", "").strip()
        note = data.get("note", "")
        count_enabled = data.get("count_enabled", True)
        aliases = data.get("aliases", [])
        group_id = data.get("group_id", None)
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
                "UPDATE member_display SET display_name=?, aliases=?, note=?, count_enabled=?, group_id=?, updated_at=datetime('now') WHERE id=?",
                (display_name, json.dumps(aliases), note, int(count_enabled), group_id, existing["id"])
            )
        else:
            conn.execute(
                "INSERT INTO member_display (raw_name, display_name, aliases, note, count_enabled, group_id, tenant_id) VALUES (?,?,?,?,?,?,?)",
                (raw_name, display_name, json.dumps(aliases), note, int(count_enabled), group_id, tenant_id)
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


# ── New: /dashboard/users/* (role-based user management) ───────────────────

def _user_can_manage(actor_role: str, target: dict) -> bool:
    """Check if actor can manage (edit/delete/toggle) target user."""
    if actor_role == "super_admin":
        return True  # can manage everyone including self
    if actor_role == "admin":
        return target.get("role") != "super_admin"
    if actor_role == "tenant_admin":
        return target.get("role") == "user"
    return False


def _allowed_create_roles(actor_role: str) -> list[dict]:
    """Roles the actor is allowed to create."""
    if actor_role == "super_admin":
        return [
            {"value": "admin", "label": "管理员"},
            {"value": "tenant_admin", "label": "租户管理员"},
            {"value": "user", "label": "用户"},
        ]
    if actor_role == "admin":
        return [
            {"value": "tenant_admin", "label": "租户管理员"},
            {"value": "user", "label": "用户"},
        ]
    if actor_role == "tenant_admin":
        return [
            {"value": "user", "label": "用户"},
        ]
    return []


@router.get("/users", response_class=HTMLResponse)
async def dashboard_users(request: Request, user: dict = Depends(require_user)):
    """User management page — role-based data visibility."""
    actor_role = user.get("role", "user")
    tenant_id = request.app.state.get_effective_tenant_id(request)

    if actor_role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")

    all_tenants = db.get_all_tenants() if actor_role in ("super_admin", "admin") else []

    # Build tenant display name map
    tenants_map = {}
    for t in all_tenants:
        tenants_map[t["id"]] = t.get("display_name") or t["id"]

    # Get users filtered by role
    users_raw = db.get_users(viewer_role=actor_role, tenant_id=tenant_id)
    users = []
    for u in users_raw:
        ud = _user_dict(u)
        ud["can_manage"] = _user_can_manage(actor_role, u)
        users.append(ud)

    can_create = len(_allowed_create_roles(actor_role)) > 0

    return _render_admin(request, "admin_center", user, "admin_users.html",
                         users=users,
                         can_create=can_create,
                         createable_roles=_allowed_create_roles(actor_role),
                         all_tenants=all_tenants,
                         tenants_map=tenants_map)


@router.post("/users/create")
async def dashboard_users_create(request: Request,
                                 username: str = Form(...),
                                 password: str = Form(...),
                                 display_name: str = Form(""),
                                 role: str = Form("user"),
                                 tenant_id: str = Form("default"),
                                 user: dict = Depends(require_user)):
    """Create a new user — role-gated."""
    actor_role = user.get("role", "user")
    allowed = {r["value"] for r in _allowed_create_roles(actor_role)}
    if role not in allowed:
        raise HTTPException(status_code=403, detail="无权创建此角色")
    try:
        uid = db.create_user(username, password, display_name, role, tenant_id=tenant_id)
        db.set_user_tenant_role(uid, tenant_id, role)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    # Audit log
    after = {"username": username, "role": role, "tenant_id": tenant_id}
    db.audit_log_action(tenant_id=tenant_id, action="user.create",
                        entity_type="user", entity_id=uid,
                        details=json.dumps(after, ensure_ascii=False))
    return RedirectResponse(url="/dashboard/users", status_code=303)


@router.post("/users/{user_id}/toggle")
async def dashboard_users_toggle(request: Request, user_id: int,
                                 user: dict = Depends(require_user)):
    """Toggle user active/inactive — role-gated."""
    actor_role = user.get("role", "user")
    target = db.get_user_by_id(user_id)
    if not target or not _user_can_manage(actor_role, target):
        raise HTTPException(status_code=403, detail="无权操作此用户")
    db.toggle_user(user_id)
    # Audit log
    refreshed = db.get_user_by_id(user_id)
    now_active = refreshed.get("is_active", 0)
    action = "user.enable" if now_active else "user.disable"
    before_after = {"username": target.get("username", ""), "is_active": bool(now_active)}
    db.audit_log_action(tenant_id=target.get("tenant_id", "default"), action=action,
                        entity_type="user", entity_id=user_id,
                        details=json.dumps(before_after, ensure_ascii=False))
    return RedirectResponse(url="/dashboard/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def dashboard_users_delete(request: Request, user_id: int,
                                 user: dict = Depends(require_user)):
    """Delete a user — role-gated."""
    actor_role = user.get("role", "user")
    target = db.get_user_by_id(user_id)
    if not target or not _user_can_manage(actor_role, target):
        raise HTTPException(status_code=403, detail="无权删除此用户")
    # Audit log before deletion
    before = {"username": target.get("username", ""), "role": target.get("role", ""), "tenant_id": target.get("tenant_id", "default")}
    db.audit_log_action(tenant_id=target.get("tenant_id", "default"), action="user.delete",
                        entity_type="user", entity_id=user_id,
                        details=json.dumps(before, ensure_ascii=False))
    db.delete_user(user_id)
    return RedirectResponse(url="/dashboard/users", status_code=303)


@router.post("/users/{user_id}/role")
async def dashboard_users_role(request: Request, user_id: int,
                               role: str = Form(...),
                               user: dict = Depends(require_user)):
    """Update user role — role-gated."""
    actor_role = user.get("role", "user")
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")

    # super_admin cannot demote self
    if actor_role == "super_admin" and user["id"] == user_id and target.get("role") == "super_admin" and role != "super_admin":
        raise HTTPException(status_code=403, detail="超级管理员不能降级自己")

    if not _user_can_manage(actor_role, target):
        raise HTTPException(status_code=403, detail="无权修改此用户的角色")

    # admin cannot create super_admin
    if actor_role == "admin" and role == "super_admin":
        raise HTTPException(status_code=403, detail="无权创建超级管理员")

    # admin cannot promote someone to same level as self
    if actor_role == "admin" and role == "admin":
        raise HTTPException(status_code=403, detail="无权将他人设为管理员")

    # tenant_admin can only set user role
    if actor_role == "tenant_admin" and role != "user":
        raise HTTPException(status_code=403, detail="无权设置此角色")

    # Ensure at least 1 super_admin remains
    if target.get("role") == "super_admin" and role != "super_admin":
        remaining = db.get_users(viewer_role="super_admin")
        if len(remaining) <= 1:
            raise HTTPException(status_code=400, detail="系统必须至少保留一个超级管理员")

    old_role = target.get("role", "")
    db.update_user(user_id, role=role)
    # Audit log
    details = {"username": target.get("username", ""), "old_role": old_role, "new_role": role,
               "operator": user.get("username", "")}
    db.audit_log_action(tenant_id=target.get("tenant_id", "default"), action="user.role_change",
                        entity_type="user", entity_id=user_id,
                        details=json.dumps(details, ensure_ascii=False))
    return RedirectResponse(url="/dashboard/users", status_code=303)


@router.post("/users/{user_id}/update")
async def dashboard_users_update(request: Request, user_id: int,
                                 user: dict = Depends(require_user)):
    """Update user fields — role-gated."""
    actor_role = user.get("role", "user")
    target = db.get_user_by_id(user_id)
    if not target or not _user_can_manage(actor_role, target):
        raise HTTPException(status_code=403, detail="无权编辑此用户")

    form = await request.form()
    kwargs = {}
    for key in ("username", "display_name", "role", "tenant_id", "telegram_chat_id"):
        val = form.get(key)
        if val is not None:
            kwargs[key] = val.strip() if isinstance(val, str) else val

    # Role re-validation
    if "role" in kwargs:
        allowed = {r["value"] for r in _allowed_create_roles(actor_role)}
        if kwargs["role"] not in allowed:
            # super_admin can still set any role, allow
            if actor_role != "super_admin":
                raise HTTPException(status_code=403, detail="无权设置此角色")

        # Protect super_admin demotion
        if target.get("role") == "super_admin" and kwargs["role"] != "super_admin":
            if user["id"] == user_id:
                raise HTTPException(status_code=403, detail="超级管理员不能降级自己")
            remaining = db.get_users(viewer_role="super_admin")
            if len(remaining) <= 1:
                raise HTTPException(status_code=400, detail="系统必须至少保留一个超级管理员")

    db.update_user_full(user_id, **kwargs)
    # Audit log
    before = {k: target.get(k, "") for k in ("username", "display_name", "role", "tenant_id") if k in kwargs}
    after = {k: kwargs[k] for k in kwargs if k in ("username", "display_name", "role", "tenant_id")}
    details = {"before": before, "after": after}
    db.audit_log_action(tenant_id=target.get("tenant_id", "default"), action="user.update",
                        entity_type="user", entity_id=user_id,
                        details=json.dumps(details, ensure_ascii=False))
    return RedirectResponse(url="/dashboard/users", status_code=303)


@router.post("/users/{user_id}/reset-password")
async def dashboard_users_reset_password(request: Request, user_id: int,
                                         new_password: str = Form(...),
                                         user: dict = Depends(require_user)):
    """Reset user password — role-gated."""
    actor_role = user.get("role", "user")
    target = db.get_user_by_id(user_id)
    if not target or not _user_can_manage(actor_role, target):
        raise HTTPException(status_code=403, detail="无权重置此用户密码")
    db.reset_user_password(user_id, new_password)
    # Audit log
    details = {"username": target.get("username", ""), "operator": user.get("username", "")}
    db.audit_log_action(tenant_id=target.get("tenant_id", "default"), action="user.reset_password",
                        entity_type="user", entity_id=user_id,
                        details=json.dumps(details, ensure_ascii=False))
    return RedirectResponse(url="/dashboard/users", status_code=303)


@router.post("/users/{user_id}/tenant")
async def dashboard_users_tenant(request: Request, user_id: int,
                                 tenant_id: str = Form(...),
                                 user: dict = Depends(require_user)):
    """Change user's tenant — role-gated (super_admin/admin only)."""
    actor_role = user.get("role", "user")
    target = db.get_user_by_id(user_id)
    if not target or not _user_can_manage(actor_role, target):
        raise HTTPException(status_code=403, detail="无权修改此用户租户")
    old_tenant = target.get("tenant_id", "")
    db.update_user_full(user_id, tenant_id=tenant_id)
    # Audit log
    details = {"username": target.get("username", ""), "old_tenant": old_tenant, "new_tenant": tenant_id,
               "operator": user.get("username", "")}
    db.audit_log_action(tenant_id=tenant_id, action="user.tenant_change",
                        entity_type="user", entity_id=user_id,
                        details=json.dumps(details, ensure_ascii=False))
    return RedirectResponse(url="/dashboard/users", status_code=303)


# ── Tenants / Audit / Settings (guarded routes) ────────────────────────────

@router.get("/tenants", response_class=HTMLResponse)
async def dashboard_tenants(request: Request, user: dict = Depends(require_user)):
    """Tenant management — super_admin only. Enhanced with stats."""
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="权限不足")
    all_tenants = db.get_all_tenants_with_inactive()
    tenants = []
    for t in all_tenants:
        td = _tenant_dict(t)
        # Attach aggregate counts
        td["user_count"] = db.count_users_by_tenant(t["id"])
        td["zoom_account_count"] = db.count_zoom_accounts_by_tenant(t["id"])
        td["channel_count"] = db.count_telegram_channels_by_tenant(t["id"])
        td["member_count"] = db.count_members_by_tenant(t["id"])
        tenants.append(td)
    return _render_admin(request, "admin_center", user, "admin_tenants.html",
                         tenants=tenants)


@router.post("/tenants/create")
async def dashboard_tenants_create(request: Request, user: dict = Depends(require_user)):
    """Create tenant — super_admin only."""
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="权限不足")
    form = await request.form()
    tenant_id = form.get("tenant_id", "").strip()
    display_name = form.get("display_name", "").strip()
    description = form.get("description", "").strip()
    # validate
    if not tenant_id or not display_name:
        raise HTTPException(status_code=400, detail="租户ID和显示名称为必填")
    if not re.match(r'^[a-z0-9_-]+$', tenant_id):
        raise HTTPException(status_code=400, detail="租户ID只能包含小写字母、数字、下划线和短横线")
    # check unique
    existing = db.get_tenant_by_id(tenant_id)
    if existing:
        raise HTTPException(status_code=400, detail="租户ID已存在")
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    import secrets
    api_token = secrets.token_hex(24)
    conn = db._get_conn()
    conn.execute(
        "INSERT INTO tenants (id, name, display_name, plan, is_active, is_global_admin, api_token, zoom_plan, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, 0, ?, 'unknown', ?, ?)",
        (tenant_id, display_name, display_name, "pro", api_token, now, now),
    )
    conn.commit()
    db.audit_log_action(tenant_id=tenant_id, action='tenant.create', entity_type='tenant', entity_id=tenant_id,
                        details=json.dumps({"tenant_id": tenant_id, "display_name": display_name, "description": description, "operator": user.get("username")}, ensure_ascii=False))
    return RedirectResponse(url="/dashboard/tenants", status_code=303)


@router.get("/tenants/{tenant_id}", response_class=HTMLResponse)
async def dashboard_tenant_detail(request: Request, tenant_id: str,
                                  user: dict = Depends(require_user)):
    """Tenant detail page — super_admin only."""
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="权限不足")
    t = db.get_tenant_by_id(tenant_id)
    if not t:
        raise HTTPException(status_code=404, detail="租户不存在")
    tenant = _tenant_dict(t)

    # Aggregate data
    user_count = db.count_users_by_tenant(tenant_id)
    zoom_account_count = db.count_zoom_accounts_by_tenant(tenant_id)
    channel_count = db.count_telegram_channels_by_tenant(tenant_id)
    member_count = db.count_members_by_tenant(tenant_id)
    alert_rule_count = db.count_alert_rules_by_tenant(tenant_id)
    enabled_alert_count = db.count_enabled_alerts_by_tenant(tenant_id)
    group_count = db.count_groups_by_tenant(tenant_id)
    zoom_accounts = db.get_tenant_zoom_accounts(tenant_id)
    bot_status = db.get_tenant_bot_status(tenant_id)

    return _render_admin(request, "admin_center", user, "tenant_detail.html",
                         tenant=tenant,
                         user_count=user_count,
                         zoom_account_count=zoom_account_count,
                         channel_count=channel_count,
                         member_count=member_count,
                         alert_rule_count=alert_rule_count,
                         enabled_alert_count=enabled_alert_count,
                         group_count=group_count,
                         zoom_accounts=zoom_accounts,
                         bot_status=bot_status)


@router.post("/tenants/{tenant_id}/toggle")
async def dashboard_tenants_toggle(request: Request, tenant_id: str,
                                   user: dict = Depends(require_role("super_admin"))):
    """Toggle tenant active/inactive."""
    db.toggle_tenant(tenant_id)
    # Redirect back to detail page if that's where we came from
    referer = request.headers.get("referer", "")
    if f"/tenants/{tenant_id}" in referer:
        return RedirectResponse(url=f"/dashboard/tenants/{tenant_id}", status_code=303)
    return RedirectResponse(url="/dashboard/tenants", status_code=303)


@router.get("/audit", response_class=HTMLResponse)
async def dashboard_audit(request: Request, user: dict = Depends(require_user)):
    """Audit log — super_admin only."""
    if user.get("role") != "super_admin":
        raise HTTPException(status_code=403, detail="权限不足")
    security_logs = db.get_security_audit_logs(limit=50)
    operation_logs = db.get_operation_audit_logs(limit=50)
    return _render_admin(request, "admin_center", user, "audit_log.html",
                         security_logs=security_logs, operation_logs=operation_logs)


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
                                user: dict = Depends(require_role("admin"))):
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
                              user: dict = Depends(require_role("admin"))):
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
                                user: dict = Depends(require_role("admin"))):
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
                               user: dict = Depends(require_role("admin"))):
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
                                user: dict = Depends(require_role("admin"))):
    """Add a monitored meeting room."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    db.create_meeting(tenant_id, 0, meeting_id, label, meeting_type)
    return RedirectResponse(url="/dashboard/admin/accounts", status_code=303)


@router.post("/admin/meetings/{meeting_db_id}/delete")
async def admin_meetings_delete(request: Request, meeting_db_id: int,
                                user: dict = Depends(require_role("admin"))):
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
                                user: dict = Depends(require_role("admin"))):
    """Create a new telegram channel."""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    db.create_tenant_channel(tenant_id, chat_id, label, is_group == "true")
    return RedirectResponse(url="/dashboard/admin/channels", status_code=303)


@router.post("/admin/channels/{channel_id}/toggle")
async def admin_channels_toggle(request: Request, channel_id: int,
                                user: dict = Depends(require_role("admin"))):
    """Toggle channel enabled/disabled."""
    db.toggle_tenant_channel(channel_id)
    return RedirectResponse(url="/dashboard/admin/channels", status_code=303)


@router.post("/admin/channels/{channel_id}/delete")
async def admin_channels_delete(request: Request, channel_id: int,
                                user: dict = Depends(require_role("admin"))):
    """Delete a channel."""
    db.delete_tenant_channel(channel_id)
    return RedirectResponse(url="/dashboard/admin/channels", status_code=303)


@router.post("/admin/channels/{channel_id}/edit")
async def admin_channels_edit(request: Request, channel_id: int,
                              label: str = Form(""),
                              chat_id: str = Form(""),
                              is_group: str = Form("false"),
                              user: dict = Depends(require_role("admin"))):
    """Edit a channel's label, chat_id, and/or is_group."""
    db.update_tenant_channel(channel_id, label=label.strip(), chat_id=chat_id.strip(),
                             is_group=(is_group == "true"))
    return RedirectResponse(url="/dashboard/admin/channels", status_code=303)


@router.post("/admin/channels/{channel_id}/test")
async def admin_channels_test(request: Request, channel_id: int,
                              user: dict = Depends(require_role("admin"))):
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


@router.get("/api/meeting-participants")
async def api_meeting_participants(request: Request, meeting_id: str,
                                    user: dict = Depends(require_user)):
    """获取指定会议的所有参与者"""
    from db import get_participants_by_meeting
    participants = get_participants_by_meeting(meeting_id)
    return JSONResponse({"ok": True, "participants": participants})


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
        "role": user.get("role", "user"),
        "tenant_id": tenant_id,
        "is_active": user.get("is_active_str", "true" if user.get("is_active") else "false"),
        "telegram_chat_id": user.get("telegram_chat_id", ""),
        "telegram_2fa_enabled": user.get("telegram_2fa_enabled", 0),
        "telegram_2fa_verified_at": user.get("telegram_2fa_verified_at", ""),
        "twofa_backup_codes": user.get("twofa_backup_codes", ""),
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
        **extra,
        "request": request,
        "active": active,
        "current_user": current_user,
        "page_title": extra.pop("title", "成员中心"),
        "is_super_admin": is_super_admin,
        "available_tenants": all_tenants,
        "current_tenant_id": tenant_id,
        "current_tenant_name": current_tenant_name,
        "hide_settings": current_user.get("role", "user") not in ("super_admin", "admin"),
        "nav_items": _get_nav_items(user.get("role", "user")),
    }
    return templates.TemplateResponse(request, template_name, context)
