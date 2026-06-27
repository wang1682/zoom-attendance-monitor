"""
Multi-tenant admin dashboard routes for Zoom Attendance Monitor.
Mounted as an APIRouter under /dashboard in the main app.
"""
import json
import re
import time
import logging
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Request, Form, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse

import db
from config import settings
from zoom_api import ZoomAPI

logger = logging.getLogger(__name__)
from zoom_metrics import ZoomMetrics
from services.auth import AuthService

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


# ── Auth helpers (compatibility wrappers for existing route Dependencies) ─────

def get_current_user(request: Request) -> dict | None:
    """Extract user info from session. Returns None if not logged in.

    Compatibility wrapper: delegates to AuthService.
    Phase 2+ will migrate routes away from this pattern.
    """
    auth = AuthService(request)
    try:
        ctx = auth.require_authenticated()
    except Exception:
        return None
    user = db.get_user_by_id(ctx.user_id)
    if not user or not user.get("is_active"):
        return None
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
        # Delegated to AuthService via the request object (injected by FastAPI)
        # FastAPI's Depends provides the request via the route handler's signature,
        # but the closure doesn't have it. We check via user dict's role field instead.
        # AuthService.require() is used in routes that pass request directly.
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

    # 运营统计（同 admin_center 数据源）
    stats = {
        "total_tenants": db.count_total_tenants(),
        "total_users": db.count_total_users(),
        "total_zoom_accounts": db.count_total_zoom_accounts(),
        "total_channels": db.count_total_channels(),
        "today_alerts": db.count_today_alerts(),
        "today_push_count": db.count_today_push_count(),
    }

    # Return skeleton HTML — /dashboard/data fills the rest via JS
    return _render_admin(request, "overview", user, "dashboard.html",
                         **{"score": score, "checks": checks, "stats": stats, "next_steps": []})

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
    """成员中心 — 实时在线 + 今日参会统计。"""
    role = user.get("role", "")

    from db import get_today_attendance_summary, get_all_groups

    tenant_id = request.app.state.get_effective_tenant_id(request)

    # ── 参数 ──
    search = request.query_params.get("search", "").strip()
    group_filter = request.query_params.get("group", "").strip()
    status_filter = request.query_params.get("status", "").strip()
    source = request.query_params.get("source", "live").strip()

    # ── 获取当前在线数据（live source） ──
    live_map = {}
    live_data = {"meetings": []}
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

    # ── 从 live meetings 或 sharing_live 确定当前会议 meeting_id ──
    current_meeting_id = None
    live_meetings = live_data.get("meetings", [])
    if live_meetings:
        # Zoom Metrics API 返回的会议 id
        mid = live_meetings[0].get("id")
        if mid:
            current_meeting_id = str(mid)
    if not current_meeting_id:
        # fallback: 从 sharing_live 当前活跃取
        try:
            from db import _get_conn
            row = _get_conn().execute(
                "SELECT meeting_id FROM sharing_live WHERE is_active=1 AND tenant_id=? ORDER BY id DESC LIMIT 1",
                (tenant_id,),
            ).fetchone()
            if row:
                current_meeting_id = str(row[0])
        except Exception:
            pass

        # ── 当前会议 session 起点：用当前会议实例的实际开始时间
    # 不使用 business day / 自然日切割。PMI 固定 meeting_id 跨天重复使用。
    session_start_after = None

    if current_meeting_id:
        try:
            # A. live meeting start_time（Zoom API）
            if live_meetings:
                for key in ("start_time", "created_time", "meeting_started_at"):
                    val = live_meetings[0].get(key)
                    if val:
                        session_start_after = str(val)
                        break

            # B. 当天该 meeting 最早的 zoom_participants enter/joined/admitted（会议真正开始时间）
            if not session_start_after:
                from datetime import timedelta as _td
                _today_myt = datetime.now(timezone.utc) + _td(hours=8)
                _today_start_utc = (_today_myt.replace(hour=0, minute=0, second=0, microsecond=0) - _td(hours=8)).isoformat()
                row = _get_conn().execute(
                    "SELECT MIN(action_time) FROM zoom_participants WHERE meeting_id=? AND tenant_id=? AND action IN ('enter','joined','admitted') AND action_time >= ?",
                    (current_meeting_id, tenant_id, _today_start_utc),
                ).fetchone()
                if row and row[0]:
                    session_start_after = str(row[0])

            # C. 当天该 meeting 最早 participant_sessions join_time
            if not session_start_after:
                from datetime import timedelta as _td2
                _tm2 = datetime.now(timezone.utc) + _td2(hours=8)
                _ts2_utc = (_tm2.replace(hour=0, minute=0, second=0, microsecond=0) - _td2(hours=8)).isoformat()
                row = _get_conn().execute(
                    "SELECT MIN(join_time_utc) FROM participant_sessions WHERE meeting_id=? AND tenant_id=? AND join_time_utc >= ?",
                    (current_meeting_id, tenant_id, _ts2_utc),
                ).fetchone()
                if row and row[0]:
                    session_start_after = str(row[0])

            # D. sharing_live 当前 active 的最早 start_time（仅当没有 zoom_participants/PS 数据时）
            if not session_start_after:
                row = _get_conn().execute(
                    "SELECT MIN(start_time) FROM sharing_live WHERE meeting_id=? AND is_active=1 AND tenant_id=? AND start_time IS NOT NULL",
                    (current_meeting_id, tenant_id),
                ).fetchone()
                if row and row[0]:
                    session_start_after = str(row[0])

            # E. ultima fallback: 最近 7 天最早 enter
            if not session_start_after:
                from datetime import timedelta as _td3
                _cutoff = (datetime.now(timezone.utc) - _td3(days=7)).isoformat()
                row = _get_conn().execute(
                    "SELECT MIN(action_time) FROM zoom_participants WHERE meeting_id=? AND action='enter' AND tenant_id=? AND action_time >= ?",
                    (current_meeting_id, tenant_id, _cutoff),
                ).fetchone()
                if row and row[0]:
                    session_start_after = str(row[0])
        except Exception:
            pass
    # ── 当前会议统计 ──
    # 用当前仍然 open 的 participant_sessions 的最早 join_time 作为会议实例开始
    # 不能按自然日/业务日切割；会议跨天时继续累计
    summary_current = None
    _session_start_after = session_start_after
    if current_meeting_id and not _session_start_after:
        try:
            _row = _get_conn().execute(
                "SELECT MIN(join_time_utc) FROM participant_sessions WHERE meeting_id=? AND tenant_id=? AND leave_time_utc IS NULL",
                (current_meeting_id, tenant_id),
            ).fetchone()
            if _row and _row[0]:
                _session_start_after = str(_row[0])
            else:
                # No open sessions — use today's earliest event
                from datetime import timedelta as _td4
                _myt_today = datetime.now(timezone.utc) + _td4(hours=8)
                _today_start_utc = (_myt_today.replace(hour=0, minute=0, second=0, microsecond=0) - _td4(hours=8)).isoformat()
                _row = _get_conn().execute(
                    "SELECT MIN(action_time) FROM zoom_participants WHERE meeting_id=? AND tenant_id=? AND action IN ('enter','joined','admitted') AND action_time >= ?",
                    (current_meeting_id, tenant_id, _today_start_utc),
                ).fetchone()
                if _row and _row[0]:
                    _session_start_after = str(_row[0])
        except Exception:
            pass
    summary = get_today_attendance_summary(
        tenant_id=tenant_id,
        meeting_id=current_meeting_id,
        session_start_after=_session_start_after,
    ) if current_meeting_id and _session_start_after else get_today_attendance_summary(tenant_id=tenant_id)

    

    members = summary.get("members", [])

    # ── 兜底：webhook 无数据时，用 Metrics 实时参与者填 ──
    from db import resolve_display_name, get_all_groups
    if not members and live_map:
        group_lookup = {}
        conn_local = db._get_conn()
        if tenant_id:
            grp_rows = conn_local.execute(
                "SELECT md.raw_name, md.display_name, COALESCE(g.name, '') AS group_name "
                "FROM member_display md "
                "LEFT JOIN member_groups g ON g.id = md.group_id AND g.tenant_id = md.tenant_id "
                "WHERE md.tenant_id = ? AND md.group_id IS NOT NULL",
                (tenant_id,),
            ).fetchall()
        else:
            grp_rows = conn_local.execute(
                "SELECT md.raw_name, md.display_name, COALESCE(g.name, '') AS group_name "
                "FROM member_display md "
                "LEFT JOIN member_groups g ON g.id = md.group_id AND g.tenant_id = md.tenant_id "
                "WHERE md.group_id IS NOT NULL"
            ).fetchall()
        for gr in grp_rows:
            for key in (gr[0].strip().lower().replace(" ", ""),
                        gr[1].strip().lower().replace(" ", "")):
                if key:
                    group_lookup[key] = gr[2]

        seen = set()
        for meeting in live_data.get("meetings", []):
            for p in meeting.get("participants", []):
                pname = p.get("name", "").strip()
                if not pname or pname in seen:
                    continue
                seen.add(pname)
                resolved = resolve_display_name(pname, tenant_id=tenant_id)
                sn = resolved["display_name"]
                key = sn.strip().lower().replace(" ", "")
                grp_name = group_lookup.get(key, "")
                # Metrics 最近活跃时间：join_time > last_activity > last_time
                metrics_last = p.get("join_time", "") or p.get("last_activity", "") or p.get("last_time", "")
                members.append({
                    "standard_name": sn,
                    "raw_name": pname,
                    "status": "online",
                    "group_name": grp_name,
                    "group_id": None,
                    "tenant_id": tenant_id or "",
                    "first_join": "—",
                    "last_leave_time": None,
                    "last_activity": metrics_last or "—",
                    "today_total_duration": "",
                    "join_count": 0,
                    "leave_count": 0,
                    "email": "",
                    "aliases": [],
                })
        # ── 补全网关 email：用全局历史最近一条 ──
        if members:
            conn_email = db._get_conn()
            for m in members:
                if m.get("email"):
                    continue
                for candidate in [m.get("standard_name", ""), m.get("raw_name", "")]:
                    if not candidate:
                        continue
                    if tenant_id:
                        row = conn_email.execute(
                            "SELECT email FROM zoom_participants WHERE LOWER(name) = LOWER(?) AND email IS NOT NULL AND email != '' AND tenant_id=? ORDER BY action_time DESC LIMIT 1",
                            (candidate, tenant_id),
                        ).fetchone()
                    else:
                        row = conn_email.execute(
                            "SELECT email FROM zoom_participants WHERE LOWER(name) = LOWER(?) AND email IS NOT NULL AND email != '' ORDER BY action_time DESC LIMIT 1",
                            (candidate,),
                        ).fetchone()
                    if row:
                        m["email"] = row[0]
                        break
        data_source = "metrics"

    # ── 合并 live 数据：在线成员状态用实时数据标记 ──
    # 注意：不覆盖 first_join / last_activity，它们来自 DB 今日事件
    from datetime import datetime, timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    def _fmt_myt(utc_val) -> str:
        """Format UTC ISO string or datetime to MM-DD HH:mm:ss MYT"""
        if not utc_val or utc_val == "—":
            return "—"
        from datetime import datetime as _dt
        if isinstance(utc_val, _dt):
            return utc_val.astimezone(MYT).strftime("%m-%d %H:%M:%S")
        try:
            s = str(utc_val).replace("Z", "+00:00")
            dt = _dt.fromisoformat(s)
            return dt.astimezone(MYT).strftime("%m-%d %H:%M:%S")
        except:
            return str(utc_val)[:16].replace("T", " ")

    for m in members:
        sn = m.get("standard_name", "")
        lp = live_map.get(sn)
        if lp:
            m["status"] = "online"
        else:
            m["status"] = "offline"


    # ── 格式化时间显示（所有成员，不受 summary_current 有无影响） ──
    for m in members:
        m["first_join_display"] = _fmt_myt(m.get("first_join", ""))
        # 在线成员离开时间显示 —（不管 DB 有没有离开记录）
        if m.get("status") == "online":
            m["last_leave_time_display"] = "—"
        else:
            m["last_leave_time_display"] = _fmt_myt(m.get("last_leave_time", ""))
        m["last_activity_display"] = _fmt_myt(m.get("last_activity", ""))

    # ── 排序辅助 ──
    def _last_activity_sort_key(val: str) -> int:
        """返回 timestamp 用于排序，无时间则返回 0 排最后"""
        if not val or val == "—":
            return 0
        try:
            s = val.replace("Z", "+00:00")
            return int(datetime.fromisoformat(s).timestamp())
        except:
            return 0

    # 排序：在线优先 → 最近活动降序 → 没有最近活动的排最后
    members.sort(key=lambda m: (
        0 if m["status"] == "online" else 1,
        -_last_activity_sort_key(m.get("last_activity", "")),
    ))
    live_online = sum(1 for m in members if m.get("status") == "online")
    live_offline = len(members) - live_online

    # DEBUG: KEAT summary state before render
    import sys
    _keat = next((m for m in members if m.get("standard_name") == "KEAT" or m.get("display_name") == "KEAT"), None)
    if _keat:
        sys.stderr.write(f"DASH_DEBUG KEAT dur={_keat.get('today_total_duration','?')} secs={_keat.get('today_total_seconds','?')} first={_keat.get('first_join','?')} last={_keat.get('last_activity','?')}\n")
    else:
        sys.stderr.write("DASH_DEBUG KEAT NOT IN MEMBERS\n")

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

    # ── member_display 映射 ──
    conn = db._get_conn()
    if tenant_id:
        md_rows = conn.execute(
            "SELECT raw_name, display_name, aliases, note, count_enabled, group_id, tenant_id FROM member_display WHERE tenant_id = ?",
            (tenant_id,)
        ).fetchall()
    else:
        md_rows = conn.execute(
            "SELECT raw_name, display_name, aliases, note, count_enabled, group_id, tenant_id FROM member_display"
        ).fetchall()
    member_displays = {}
    for r in md_rows:
        raw_name = r[0]
        disp_name = r[1]
        aliases = json.loads(r[2]) if r[2] else []
        md_tenant = r[6] or ""
        entry = {
            "display_name": disp_name,
            "aliases": aliases,
            "note": r[3] or "",
            "count_enabled": bool(r[4]),
            "group_id": r[5],
            "raw_name": raw_name,
            "tenant_id": md_tenant,
        }
        # 以 (tenant_id, name_key) 为 key，避免跨租户覆盖
        for name_key in (raw_name.strip().lower().replace(" ", ""),
                         disp_name.strip().lower().replace(" ", "")):
            if name_key:
                member_displays[(md_tenant, name_key)] = entry

    for m in members:
        sn = m.get("standard_name", "")
        m_tenant = m.get("tenant_id", "")
        key = sn.strip().lower().replace(" ", "")
        # 精确匹配：用 (成员tenant, name_key)
        md_entry = member_displays.get((m_tenant, key), {})
        if not md_entry and m_tenant:
            # 降级：如果成员 tenant 有值但没匹配到，尝试空 tenant
            md_entry = member_displays.get(("", key), {})
        if not md_entry:
            # 降级：任意租户
            for (t, k), v in member_displays.items():
                if k == key:
                    md_entry = v
                    break
        m["raw_name"] = md_entry.get("raw_name", sn)
        # 如果 summary 已经填充了 group_id，不覆盖；没有才从 member_displays 取
        if m.get("group_id") is None:
            m["group_id"] = md_entry.get("group_id")

    return _render_admin(request, "participants", user, "participants.html",
                         title="成员中心",
                         members=members,
                         online_count=live_online,
                         offline_count=live_offline,
                         groups=all_groups,
                         search=search,
                         group_filter=group_filter,
                         status_filter=status_filter,
                         tenant_id=tenant_id,
                         data_source=data_source,
                         is_realtime=is_realtime,
                         source=source,
                         metrics_online=metrics_online,
                         current_meeting_id=current_meeting_id,
                         session_start_after=_session_start_after)


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
    import zoneinfo
    mytz = zoneinfo.ZoneInfo("Asia/Kuala_Lumpur")
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
    from db import get_meeting_history, get_sharing_records, get_zoom_accounts, _myt_short, _fmt_dur
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
                    "participants": participants,
                })
    except Exception:
        pass

    show_test = request.query_params.get("show_test", "0") == "1"
    history, total_meetings = get_meeting_history(tenant_id, limit=100, offset=0, show_test=show_test)
    sharing_search = request.query_params.get("search", "")
    sharing_group_id = request.query_params.get("group_id", "")
    sharing, sharing_total, sharing_meta = get_sharing_records(
        tenant_id, limit=500,
        start_time=sharing_start_utc,
        end_time=sharing_end_utc,
        search=sharing_search or None,
        group_id=sharing_group_id or None,
    )

    # ── 合并重复共享记录：按 user_name + content 分组 ──
    from collections import OrderedDict
    sharing_grouped = OrderedDict()
    for s in sharing:
        # 跳过无 user_name 的记录
        uname = s.get("user_name", "").strip()
        content = s.get("content", "desktop").strip()
        if not uname:
            continue
        name_key = (
            uname.lower()
            .replace(" ", "")
            .replace("_", "")
            .replace("-", "")
        )
        grp_key = name_key
        if grp_key not in sharing_grouped:
            sharing_grouped[grp_key] = {
                "user_name": uname,  # 保留第一个出现的原始大小写
                "group_name": s.get("group_name", ""),
                "group_id": s.get("group_id", ""),
                "content": content,
                "first_start": s.get("start_time", ""),
                "last_end": s.get("end_time", ""),
                "total_seconds": 0,
                "count": 0,
                "is_active": False,
                "details": [],
            }
        g = sharing_grouped[grp_key]
        g["count"] += 1
        # 更新时间范围
        if s.get("start_time") and (not g["first_start"] or s["start_time"] < g["first_start"]):
            g["first_start"] = s["start_time"]
        if s.get("end_time") and (not g["last_end"] or s["end_time"] > g["last_end"]):
            g["last_end"] = s["end_time"]
        elif s.get("is_active"):
            g["last_end"] = ""  # 有活跃记录则显示空（共享中）
            g["is_active"] = True
        # 累加时长
        dur_sec = s.get("duration_seconds", 0) or 0
        g["total_seconds"] += dur_sec
        # 保留明细
        g["details"].append({
            "start_time": s.get("start_time", ""),
            "start_time_display": s.get("start_time_display", ""),
            "end_time": s.get("end_time", ""),
            "end_time_display": s.get("end_time_display", ""),
            "duration": s.get("duration", ""),
            "is_active": s.get("is_active", False),
        })
    # 转列表，添加时长格式化
    sharing_grouped_list = []
    for grp in sharing_grouped.values():
        grp["first_start_display"] = _myt_short(grp["first_start"])
        if grp["last_end"]:
            grp["last_end_display"] = _myt_short(grp["last_end"])
        else:
            grp["last_end_display"] = "共享中" if grp["is_active"] else ""
        grp["total_duration"] = _fmt_dur(grp["total_seconds"])
        sharing_grouped_list.append(grp)

    # ── 排序：共享中优先 > 最后开始最新 > 总时长最长 ──
    def _sort_ts(v):
        try:
            return datetime.fromisoformat(str(v).replace("Z", "+00:00")).timestamp()
        except Exception:
            return 0
    sharing_grouped_list.sort(
        key=lambda g: (
            0 if g.get("is_active") else 1,
            -_sort_ts(g.get("first_start")),
            -(g.get("total_seconds") or 0),
        )
    )
    sharing_grouped_total = len(sharing_grouped_list)

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

    # ── Official summary (for tab=official) ──
    from app import fmt_myt
    from db import get_official_session_summary as _get_official_summary
    _search = request.query_params.get('search', '')
    _date_from = request.query_params.get('date_from', '')
    _date_to = request.query_params.get('date_to', '')
    _raw_summary = _get_official_summary(tenant_id, date_from=_date_from or None, date_to=_date_to or None)

    # ── 构建 member_display 名→主名映射 ──
    import db as _db
    import json as _json
    _mconn = _db._get_conn()
    _name_map = {}          # participant_name → canonical_name
    _canonical_aliases = {} # canonical_name → [alias1, alias2, ...]
    _canonical_emails = {}  # canonical_name → {set of emails}
    _md_rows = _mconn.execute(
        "SELECT display_name, raw_name, match_key, aliases "
        "FROM member_display WHERE tenant_id=? AND deleted=0",
        (tenant_id,)
    ).fetchall()

    # Step 1: 预构建 alias → canonical 字典
    # 先建立 display_name/raw_name/match_key 的自映射
    from db import normalize_member_name as _nmn
    _alias_to_canonical = {}  # normalize(key) → canonical display_name
    _canonical_aliases = {}   # canonical_name → [alias1, alias2, ...]
    _canonical_emails = {}    # canonical_name → {set of emails}
    for md in _md_rows:
        _canonical = md["display_name"]
        for k in [_canonical, md["raw_name"], md["match_key"]]:
            if k:
                nk = _nmn(k)[1]  # 使用完整 normalize（去Host、全角半角、去空格等）
                if nk and nk not in _alias_to_canonical:
                    _alias_to_canonical[nk] = _canonical
        # 额外注册 match_key 原始值（不经 normalize），用于 member_display 中 match_key 与预期不同的情形
        _mk_raw = md["match_key"]
        if _mk_raw and _mk_raw not in _alias_to_canonical:
            _alias_to_canonical[_mk_raw] = _canonical
    # 再处理 aliases JSON，不覆盖已有映射
    for md in _md_rows:
        _canonical = md["display_name"]
        try:
            al = _json.loads(md["aliases"]) if md["aliases"] else []
            if isinstance(al, list):
                for a in al:
                    nk = _nmn(a)[1]
                    if nk and nk not in _alias_to_canonical:
                        _alias_to_canonical[nk] = _canonical
        except (ValueError, TypeError):
            pass

    # Step 2: 注册 raw_name → display 精确映射（用于 match_key 覆盖 normalize 规则的情形）
    _raw_name_to_display = {}
    for md in _md_rows:
        _rn = md["raw_name"]
        _disp = md["display_name"]
        if _rn and _rn != _disp:
            # 只有 raw_name → display 有变动时才注册
            _raw_name_to_display[_rn] = _disp

    # Step 2: 预构建 canonical_name → 别名列表
    for md in _md_rows:
        cname = md["display_name"]
        if cname not in _canonical_aliases:
            _canonical_aliases[cname] = []
        if md["raw_name"] and md["raw_name"] != cname:
            rn = md["raw_name"]
            if rn not in _canonical_aliases[cname]:
                _canonical_aliases[cname].append(rn)
        try:
            al = _json.loads(md["aliases"]) if md["aliases"] else []
            if isinstance(al, list):
                for a in al:
                    if a != cname and a not in _canonical_aliases[cname]:
                        _canonical_aliases[cname].append(a)
        except (ValueError, TypeError):
            pass

    def _resolve_canonical(pname):
        """通过 alias_to_canonical + member_key 映射查找"""
        if not pname:
            return pname
        # Step 0: 精确 raw_name 查 member_display（优先于 normalize，支持手动拆分）
        _rd = _raw_name_to_display.get(pname)
        if _rd:
            return _rd
        # Step 0: 内置 alias 映射（同 db.py make_identity_key 保持一致）
        _ALIAS = {
            "antheafk": "anthea",
            "harysonharyson": "haryson",
            "crispin": "crispini",
            "dcyoungest": "youngest",
            "dcoceanus": "oceanus",
        }
        # Step 1: 完整 normalize 后的 member_key 查别名
        mk = _nmn(pname)[1]
        mk = _ALIAS.get(mk, mk)
        found = _alias_to_canonical.get(mk)
        if found:
            return found
        # Step 2: member_key 查首次出现的最常见名称
        found = _member_key_to_canonical.get(mk)
        if found:
            return found
        return pname

    def _get_member_key(name: str) -> str:
        from db import normalize_member_name as _nmn
        return _nmn(name)[1]

    # ────────────────────────────────────────────────
    # Step 3: 预构建 member_key → display_name 映射（来自 _raw_summary 的最常见写法）
    # ────────────────────────────────────────────────
    _member_key_to_canonical = {}   # member_key → 最常出现/首条 canonical_name
    _raw_name_to_member_key = {}    # raw_name → member_key
    for r in _raw_summary:
        pname = r.get("participant_name", "")
        if not pname:
            continue
        mk = _get_member_key(pname)
        _raw_name_to_member_key[pname] = mk
        # 以该组中第一条记录的主显示名作为 canonical
        if mk not in _member_key_to_canonical:
            _member_key_to_canonical[mk] = pname

    # 也注册 member_key 缓存（如果 raw_name 本身就在 member_key_to_canonical 中则直接使用）
    # 确保 _resolve_canonical 能通过 raw_name 找到 member_key 映射
    for r in _raw_summary:
        pname = r.get("participant_name", "")
        if pname:
            mk = _raw_name_to_member_key[pname]
            canon = _member_key_to_canonical.get(mk, pname)
            if canon != pname:
                # 注册 pname → canon 到 _alias_to_canonical（让 _resolve_canonical 命中）
                nk = pname.lower().replace(" ", "")
                if nk not in _alias_to_canonical:
                    _alias_to_canonical[nk] = canon

    # Step 3: 按 canonical_name 聚合 _raw_summary
    _canonical_groups = {}  # canonical_name → {sessions, dur, first, last, emails, aliases, raw_names}
    for r in _raw_summary:
        pname = r.get("participant_name", "")
        cname = _resolve_canonical(pname)
        if cname not in _canonical_groups:
            _canonical_groups[cname] = {
                "session_count": 0,
                "total_duration_minutes": 0,
                "first_join": None,
                "last_leave": None,
                "emails": set(),
                "raw_names": set(),
            }
        g = _canonical_groups[cname]
        g["session_count"] += r.get("session_count", 0)
        g["total_duration_minutes"] += r.get("total_duration_minutes", 0) or 0
        fj = r.get("first_join")
        ll = r.get("last_leave")
        if fj and (g["first_join"] is None or fj < g["first_join"]):
            g["first_join"] = fj
        if ll and (g["last_leave"] is None or ll > g["last_leave"]):
            g["last_leave"] = ll
        if r.get("email"):
            g["emails"].add(r["email"])
        g["raw_names"].add(pname)

    _members_list = []
    _total_sessions = 0
    _earliest = None
    _latest = None

    # ── 补充实时数据到 canonical_groups ──
    if _canonical_groups:
        _import_conn2 = _db._get_conn()
        for _cname, _g in list(_canonical_groups.items()):
            _rn = list(_g["raw_names"])
            # 如果 canonical_name 不在 raw_names 中，加上去
            if _cname not in _rn:
                _rn.insert(0, _cname)
            _ph = ", ".join(["?" for _ in _rn])
            _rt_row = _import_conn2.execute(
                f"""
                SELECT
                    MIN(action_time) as first_time,
                    MAX(action_time) as last_time
                FROM zoom_participants
                WHERE name IN ({_ph})
                  AND action IN ('enter','leave','joined','left')
                """,
                _rn,
            ).fetchone()
            if _rt_row:
                _rt_first = _rt_row["first_time"]
                _rt_last = _rt_row["last_time"]
                if _rt_first and (not _g["first_join"] or _rt_first < _g["first_join"]):
                    _g["first_join"] = _rt_first
                if _rt_last and (not _g["last_leave"] or _rt_last > _g["last_leave"]):
                    _g["last_leave"] = _rt_last
        del _import_conn2

    # ── 考勤视角帮助函数 ──
    def _fmt_dur_cn(total_min):
        """分钟 → X天X小时格式 (中文)"""
        total_min = total_min or 0
        if total_min >= 1440:
            d = total_min // 1440
            h = (total_min % 1440) // 60
            return f"{d}天{h}小时" if h else f"{d}天"
        elif total_min >= 60:
            h = total_min // 60
            m = total_min % 60
            return f"{h}小时{m}分" if m else f"{h}小时"
        else:
            return f"{total_min}分"

    def _fmt_dur_min(total_min):
        total_min = total_min or 0
        if total_min >= 1440:
            d = total_min // 1440
            h = (total_min % 1440) // 60
            m = total_min % 60
            return f"{d}d {h}h {m}m" if m else f"{d}d {h}h"
        elif total_min >= 60:
            h = total_min // 60
            m = total_min % 60
            return f"{h}h {m}m" if m else f"{h}h"
        else:
            return f"{total_min}m"

    def _calc_attendance_days(_tid, _cname, _raw_names_set):
        """从实时 + 官方数据计算出勤天数和累计时长（按 MYT 日期去重）"""
        _import_conn = _db._get_conn()
        _names = [_cname] + list(_raw_names_set)
        _placeholders = ", ".join(["?" for _ in _names])

        # 1. 实时数据：zoom_participants 中 enter→leave 配对，按 MYT 日期计算时长
        _realtime_sql = f"""
            WITH paired AS (
                SELECT
                    LAG(action_time) OVER (PARTITION BY name, meeting_id ORDER BY action_time) AS prev_time,
                    LAG(action) OVER (PARTITION BY name, meeting_id ORDER BY action_time) AS prev_action,
                    action_time,
                    action,
                    name
                FROM zoom_participants
                WHERE name IN ({_placeholders})
                  AND action IN ('enter','leave','joined','left')
            )
            SELECT
                SUBSTR(prev_time, 1, 10) as utc_date,
                CAST(ROUND(SUM(
                    CASE WHEN prev_action IN ('enter','joined') AND action IN ('leave','left')
                         THEN (julianday(action_time) - julianday(prev_time)) * 86400.0 / 60.0
                         ELSE 0 END
                )) AS INTEGER) as day_minutes
            FROM paired
            WHERE prev_action IN ('enter','joined')
              AND action IN ('leave','left')
            GROUP BY utc_date
        """
        _realtime_rows = _import_conn.execute(_realtime_sql, _names).fetchall()

        # 2. 官方数据：official_attendance_sessions
        _official_sql = f"""
            SELECT
                SUBSTR(join_time, 1, 10) as utc_date,
                SUM(duration_minutes) as day_minutes
            FROM official_attendance_sessions
            WHERE tenant_id = ?
              AND ({ " OR ".join(["LOWER(participant_name) = LOWER(?)" for _ in _names]) })
            GROUP BY utc_date
        """
        _official_rows = _import_conn.execute(_official_sql, [_tid] + _names).fetchall()

        # 3. 合并：按 utc_date 去重，取 max 时长
        _merged = {}
        for _r in _realtime_rows:
            _d = _r["utc_date"]
            _merged[_d] = max(_merged.get(_d, 0), _r["day_minutes"] or 0)
        for _r in _official_rows:
            _d = _r["utc_date"]
            _merged[_d] = max(_merged.get(_d, 0), _r["day_minutes"] or 0)

        _total_min = sum(_merged.values())
        _days = len(_merged)
        return {"days": _days, "total_minutes": _total_min, "duration_display": _fmt_dur_min(_total_min)}

    def _calc_last_active(_tid, _cname, _raw_names_set):
        """从实时 + 官方数据查最近出勤，返回人性化时间（实时优先）"""
        _import_conn = _db._get_conn()
        _names = [_cname] + list(_raw_names_set)
        _placeholders = ", ".join(["?" for _ in _names])

        # 1. 实时数据：查最后一条 enter/leave 事件
        _rt = _import_conn.execute(
            f"""
            SELECT MAX(action_time) as last_time
            FROM zoom_participants
            WHERE name IN ({_placeholders})
              AND action IN ('enter','leave','joined','left')
            """,
            _names,
        ).fetchone()
        _rt_ts = _rt["last_time"] if _rt else None

        # 1b. current_member_sessions 补查（正在进行的会话）
        _ms = _import_conn.execute(
            f"""
            SELECT MAX(last_activity_at) as last_activity
            FROM current_member_sessions
            WHERE display_name IN ({_placeholders})
            """,
            _names,
        ).fetchone()
        _ms_ts = _ms["last_activity"] if _ms else None

        # 2. 官方数据：查最后 join_time
        _like_clauses2 = " OR ".join(["LOWER(participant_name) = LOWER(?)" for _ in _names])
        _of = _import_conn.execute(
            f"""
            SELECT MAX(join_time) as last_join
            FROM official_attendance_sessions
            WHERE tenant_id = ?
              AND ({_like_clauses2})
            """,
            [_tid] + _names,
        ).fetchone()
        _of_ts = _of["last_join"] if _of else None

        # 3. 取所有来源中较新的那个
        _candidates = [t for t in [_rt_ts, _ms_ts, _of_ts] if t]
        _raw_ts = max(_candidates) if _candidates else None
        if not _raw_ts:
            return "—"

        # 4. 格式化为友好时间
        if callable(fmt_myt):
            myt_str = fmt_myt(_raw_ts)
            import datetime as _dt
            _now = _dt.datetime.now(_dt.timezone.utc)
            _now_myt = _now.astimezone(_dt.timezone(_dt.timedelta(hours=8)))
            try:
                _ts = _dt.datetime.strptime(myt_str, "%m-%d %H:%M:%S").replace(
                    year=_now_myt.year,
                    tzinfo=_dt.timezone(_dt.timedelta(hours=8))
                )
                _diff = _now_myt - _ts
                if _diff.days == 0:
                    return f"今天 {myt_str[6:11]}"
                elif _diff.days == 1:
                    return f"昨天 {myt_str[6:11]}"
                elif _diff.days <= 7:
                    return f"{_diff.days}天前 {myt_str[6:11]}"
            except:
                pass
            return myt_str[:16]
        return str(_raw_ts)[:16]


    def _calc_official_last(_tid, _cname, _raw_names_set):
        """仅查官方最后时间（用于对比显示）"""
        _import_conn = _db._get_conn()
        _names = [_cname] + list(_raw_names_set)
        _like_clauses = " OR ".join(["LOWER(participant_name) = LOWER(?)" for _ in _names])
        _of = _import_conn.execute(
            f"""
            SELECT MAX(join_time) as last_join
            FROM official_attendance_sessions
            WHERE tenant_id = ?
              AND ({_like_clauses})
            """,
            [_tid] + _names,
        ).fetchone()
        _of_ts = _of["last_join"] if _of else None
        if not _of_ts:
            return None
        if callable(fmt_myt):
            myt_str = fmt_myt(_of_ts)
            import datetime as _dt
            _now = _dt.datetime.now(_dt.timezone.utc)
            _now_myt = _now.astimezone(_dt.timezone(_dt.timedelta(hours=8)))
            try:
                _ts = _dt.datetime.strptime(myt_str, "%m-%d %H:%M:%S").replace(
                    year=_now_myt.year,
                    tzinfo=_dt.timezone(_dt.timedelta(hours=8))
                )
                _diff = _now_myt - _ts
                if _diff.days == 0:
                    return f"今天 {myt_str[6:11]}"
                elif _diff.days == 1:
                    return f"昨天 {myt_str[6:11]}"
                elif _diff.days <= 7:
                    return f"{_diff.days}天前 {myt_str[6:11]}"
            except:
                pass
            return myt_str[:16]
        return str(_of_ts)[:16]


    for cname, g in sorted(_canonical_groups.items()):
        _total_sessions += g["session_count"]
        _earliest_tmp = g["first_join"]
        _latest_tmp = g["last_leave"]
        dur_min = g["total_duration_minutes"]
        dur_disp = _fmt_dur_cn(dur_min)

        # 搜索过滤：搜索词匹配 canonical_name 或别名
        if _search:
            _search_lower = _search.lower()
            _matches_search = (
                _search_lower in cname.lower()
                or any(_search_lower in (a or "").lower() for a in _canonical_aliases.get(cname, []))
                or any(_search_lower in (rn or "").lower() for rn in g["raw_names"])
            )
            if not _matches_search:
                continue

        # 构建别名列表（去重，排除主名）
        _all_aliases = list(g["raw_names"] - {cname})
        for a in _canonical_aliases.get(cname, []):
            if a != cname and a not in _all_aliases:
                _all_aliases.append(a)
        # 排序：短的在前
        _all_aliases.sort(key=lambda x: (len(x), x))

        _email = ", ".join(sorted(g["emails"])) if g["emails"] else ""

        # 计算考勤视角指标：出勤天数、日均时长、最近出勤
        _attendance_days = _calc_attendance_days(tenant_id, cname, g["raw_names"])
        _last_active = _calc_last_active(tenant_id, cname, g["raw_names"])
        _avg_daily_disp = _fmt_dur_min(_attendance_days["total_minutes"] // _attendance_days["days"]) if _attendance_days["days"] > 0 else "0m"

        # 纯官方 last_seen
        _official_last = _calc_official_last(tenant_id, cname, g["raw_names"])

        _members_list.append(dict(
            name=cname,
            aliases=_all_aliases,
            email=_email,
            session_count=g["session_count"],
            total_duration_minutes=dur_min,
            total_duration_display=dur_disp,
            first_join=_earliest_tmp,
            first_join_display=fmt_myt(_earliest_tmp) if callable(fmt_myt) else (_earliest_tmp or "—"),
            last_leave=_latest_tmp,
            last_leave_display=fmt_myt(_latest_tmp) if callable(fmt_myt) else (_latest_tmp or "—"),
            attendance_days=_attendance_days["days"],
            total_duration_display_attendance=_attendance_days["duration_display"],
            avg_daily_display=_avg_daily_disp,
            last_active_display=_last_active,
            official_last_display=_official_last,
        ))

    # 顶层统计：考勤视角
    _total_attendance_days = sum(m.get("attendance_days", 0) for m in _members_list)
    _total_attendance_min = sum(m.get("total_duration_minutes", 0) for m in _members_list)
    _avg_daily_total = _fmt_dur_min(_total_attendance_min // _total_attendance_days) if _total_attendance_days > 0 else "0m"
    _last_active_overall = max((m.get("last_active_display", "") for m in _members_list if m.get("last_active_display") and m["last_active_display"] != "—"), default="—")

    summary_official = {
        "member_count": len(_members_list),
        "total_sessions": _total_sessions,
        "attendance_days": _total_attendance_days,
        "total_duration_display": _fmt_dur_min(_total_attendance_min),
        "avg_daily_display": _avg_daily_total,
        "last_active_display": _last_active_overall,
        "members": _members_list,
    }
    # ── Imported files ──
    import db as _db
    _conn = _db._get_conn()
    imported_files = [
        dict(r)
        for r in _conn.execute(
            """SELECT source_file, COUNT(*) as row_count,
                      MIN(imported_at) as imported_at,
                      COUNT(DISTINCT participant_name) as participant_count,
                      SUM(duration_minutes) as total_minutes
               FROM official_attendance_sessions
               WHERE tenant_id = ?
               GROUP BY source_file
               ORDER BY imported_at DESC""",
            (tenant_id,),
        ).fetchall()
    ]
    for f in imported_files:
        f["imported_at_display"] = fmt_myt(f.get("imported_at")) if callable(fmt_myt) else (f.get("imported_at","—"))

    return _render_admin(request, "meetings", user, "meetings.html",
                         title="会议中心",
                         live_meetings=live,
                         history_meetings=history,
                         live_count=len(live) if live else 0,
                         history_count=len(history) if history else 0,
                         sharing_count=len(sharing) if sharing else 0,
                         total_meetings=total_meetings,
                         sharing_records=sharing,
                         sharing_total=sharing_total,
                         sharing_grouped=sharing_grouped_list,
                         sharing_grouped_total=sharing_grouped_total,
                         sharing_range_label=range_label,
                         sharing_range=range_val,
                         sharing_start=start_param,
                         sharing_end=end_param,
                         sharing_search=sharing_search,
                         sharing_group_id=sharing_group_id,
                         sharing_groups=groups_stats,
                         summary=dict(official=summary_official, imported_files=imported_files),
                         tab=tab,
                         imported_files=imported_files)


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
    """Settings page — system configuration (super_admin only)."""
    role = user.get("role", "")
    if role != "super_admin":
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
    """Save system settings (super_admin only)."""
    role = user.get("role", "")
    if role != "super_admin":
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
                fresh_user = db.get_user_by_id(user["id"])
                chat_id = fresh_user.get("telegram_chat_id", "") if fresh_user else ""

            if not chat_id:
                return JSONResponse(status_code=400, content={
                    "ok": False, "message": "当前用户未绑定 Telegram。请先在安全中心绑定 Telegram 后再测试。"
                })

            # Send test message via TelegramService
            msg = (
                "🧪 *Zoom Monitor 测试推送*\n\n"
                "这是一条来自系统设置的测试消息。\n"
                f"发送时间: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')} UTC\n"
                f"Bot: @{bot_username}"
            )
            from services.telegram import TelegramService
            tg = TelegramService(token=bot_token, chat_id=chat_id)
            result = await tg.send_async(msg, parse_mode="Markdown")
            if result.get("ok"):
                return JSONResponse(content={
                    "ok": True, "message": f"✅ 测试推送成功！已发送至 @{bot_username}"
                })
            else:
                return JSONResponse(status_code=400, content={
                    "ok": False, "message": f"发送失败: {result.get('error', 'unknown')}"
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

    return _render_admin(request, "admin_center", user, "admin_center.html")


# ── Admin: Tenants ────────────────────────────────────────────────────────────

@router.get("/admin/tenants", response_class=HTMLResponse)
async def admin_tenants(request: Request, user: dict = Depends(require_user)):
    """Tenant management page — super_admin only."""
    role = user.get("role", "")
    if role != "super_admin":
        raise HTTPException(status_code=403, detail="权限不足")
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
async def update_member_display_api(request: Request, user: dict = Depends(require_role("user"))):
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
    """Create a new user. Only super_admin and admin can create users."""
    actor_role = user.get("role", "")
    if actor_role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")
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
    """Toggle user active/inactive. Only super_admin and admin can toggle users."""
    actor_role = user.get("role", "")
    if actor_role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")
    db.toggle_user(user_id)
    return RedirectResponse(url="/dashboard/admin/users", status_code=303)


@router.post("/admin/users/{user_id}/delete")
async def admin_users_delete(request: Request, user_id: int,
                             user: dict = Depends(require_user)):
    """Delete a user — super_admin only."""
    actor_role = user.get("role", "")
    if actor_role != "super_admin":
        raise HTTPException(status_code=403, detail="仅超级管理员可删除用户")
    db.delete_user(user_id)
    return RedirectResponse(url="/dashboard/admin/users", status_code=303)


# ── New: /dashboard/users/* (role-based user management) ───────────────────

def _user_can_manage(actor_role: str, target: dict) -> bool:
    """Check if actor can manage target user.

    Phase 1 compatibility: used by existing routes that pass (actor_role, target).
    Routes will be migrated to AuthService(request).can_manage(target) in Phase 2+.
    """
    try:
        return AuthService._check_role_can_manage(actor_role, target)
    except Exception:
        return False


def _allowed_create_roles(actor_role: str) -> list[dict]:
    """Roles the actor is allowed to create.

    Phase 1 compatibility wrapper.
    Will be replaced by AuthService(request).allowed_create_roles() in Phase 2+.
    """
    return AuthService._check_allowed_create_roles(actor_role)

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
    """Delete a user — super_admin only."""
    actor_role = user.get("role", "user")
    if actor_role != "super_admin":
        raise HTTPException(status_code=403, detail="仅超级管理员可删除用户")
    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")
    # Audit log before deletion
    before = {"username": target.get("username", ""), "role": target.get("role", ""), "tenant_id": target.get("tenant_id", "default")}
    db.audit_log_action(tenant_id=target.get("tenant_id", "default"), action="user.delete",
                        entity_type="user", entity_id=user_id,
                        details=json.dumps(before, ensure_ascii=False))
    db.delete_user(user_id)
    return RedirectResponse(url="/dashboard/users", status_code=303)


@router.get("/users/{user_id}/role")
@router.get("/users/{user_id}/tenant")
async def dashboard_users_post_only(request: Request, user_id: int,
                                    user: dict = Depends(require_user)):
    """GET returns friendly page instead of 405."""
    return _render_admin(request, "admin_center", user, "post_only.html",
                         user_id=user_id, title="请使用弹窗操作")

@router.post("/users/{user_id}/role")
async def dashboard_users_role(request: Request, user_id: int,
                               role: Optional[str] = Form(None),
                               new_role: Optional[str] = Form(None),
                               user: dict = Depends(require_user)):
    """Update user role — role-gated. Accepts role or new_role."""
    role = role or new_role
    if not role:
        raise HTTPException(status_code=422, detail="缺少 role 参数")
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

    # user (tenant-level) can only set user role
    if actor_role == "user" and role != "user":
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


@router.post("/users/{user_id}/tg-2fa")
async def dashboard_users_tg2fa(request: Request, user_id: int,
                                 user: dict = Depends(require_user)):
    """Admin bind/unbind Telegram 2FA for a user."""
    import json as _json2
    actor_role = user.get("role", "user")
    if actor_role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")

    target = db.get_user_by_id(user_id)
    if not target:
        raise HTTPException(status_code=404, detail="用户不存在")

    body = await request.json()
    chat_id = body.get("chat_id", "")

    if chat_id:
        # Bind: set telegram_chat_id + enable 2FA
        db.update_user_full(user_id, telegram_chat_id=chat_id, telegram_2fa_enabled=1,
                            telegram_2fa_verified_at=None)
        db.audit_log_action(
            tenant_id=target.get("tenant_id", "default"),
            action="enable_telegram_2fa", entity_type="user", entity_id=user_id,
            details=_json2.dumps({"target": target.get("username", ""), "operator": user.get("username", "")}, ensure_ascii=False)
        )
        return {"ok": True, "detail": "TG 两步验证已启用"}
    else:
        # Unbind: clear chat_id + disable 2FA
        db.update_user_full(user_id, telegram_chat_id="", telegram_2fa_enabled=0,
                            telegram_2fa_verified_at=None)
        db.audit_log_action(
            tenant_id=target.get("tenant_id", "default"),
            action="disable_telegram_2fa", entity_type="user", entity_id=user_id,
            details=_json2.dumps({"target": target.get("username", ""), "operator": user.get("username", "")}, ensure_ascii=False)
        )
        return {"ok": True, "detail": "TG 两步验证已解绑"}


@router.post("/users/{user_id}/tenant")
async def dashboard_users_tenant(request: Request, user_id: int,
                                 tenant_id: Optional[str] = Form(None),
                                 new_tenant: Optional[str] = Form(None),
                                 role: Optional[str] = Form(None),
                                 user: dict = Depends(require_user)):
    """Change user's tenant and/or role — role-gated (super_admin/admin only)."""
    import json as _json2
    # Parse JSON body if applicable
    content_type = request.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = await request.json()
            tenant_id = body.get("tenant_id", body.get("new_tenant", tenant_id))
            role = body.get("role", role)
        except Exception:
            pass
    tenant_id = tenant_id or new_tenant
    if not tenant_id and not role:
        return JSONResponse({"detail": "缺少 tenant_id 和 role，至少提供一个"}, status_code=422)
    actor_role = user.get("role", "user")
    target = db.get_user_by_id(user_id)
    if not target or not _user_can_manage(actor_role, target):
        raise HTTPException(status_code=403, detail="无权修改此用户")
    details = {"username": target.get("username", "")}
    if tenant_id:
        old_tenant = target.get("tenant_id", "")
        db.update_user_full(user_id, tenant_id=tenant_id)
        details["old_tenant"] = old_tenant
        details["new_tenant"] = tenant_id
    if role:
        valid_roles = {"super_admin", "admin", "user", "viewer"}
        if role not in valid_roles:
            return JSONResponse({"detail": f"无效角色: {role}"}, status_code=422)
        if actor_role == "admin" and role == "super_admin":
            raise HTTPException(status_code=403, detail="无权创建超级管理员")
        if actor_role == "user" and role not in ("user",):
            raise HTTPException(status_code=403, detail="租户管理员仅可设置为普通用户或租户管理员")
        old_role = target.get("role", "")
        db.update_user_full(user_id, role=role)
        details["old_role"] = old_role
        details["new_role"] = role
    db.audit_log_action(
        tenant_id=tenant_id or target.get("tenant_id", "default"),
        action="user.update", entity_type="user", entity_id=user_id,
        details=_json2.dumps(details, ensure_ascii=False)
    )
    return {"ok": True, "detail": "保存成功"}


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
    role = user.get("role", "")
    if role not in ("super_admin", "admin"):
        raise HTTPException(status_code=403, detail="权限不足")
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
async def admin_channels(request: Request, user: dict = Depends(require_role("admin"))):
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
    from services.telegram import TelegramService
    tg = TelegramService(token=token)
    result = tg.send("✅ 测试消息 — 推送配置正常，机器人已接入", chat_id=target["chat_id"])
    return JSONResponse(result)


@router.get("/api/meeting-participants")
async def api_meeting_participants(request: Request, meeting_id: str,
                                    user: dict = Depends(require_user)):
    """获取指定会议的统计详情"""
    from db import get_participants_by_meeting
    detail = get_participants_by_meeting(meeting_id)
    return JSONResponse({"ok": True, **detail})


@router.get("/api/sharing-stats")
async def api_sharing_stats(request: Request, user: dict = Depends(require_user)):
    """获取共享统计（顶部卡片）"""
    from db import get_sharing_day_stats
    tenant_id = request.app.state.get_effective_tenant_id(request)
    return JSONResponse({"ok": True, **get_sharing_day_stats(tenant_id)})


@router.get("/api/sharing-trend")
async def api_sharing_trend(request: Request, hours: int = 24,
                            user: dict = Depends(require_user)):
    """获取共享趋势（按小时）"""
    from db import get_sharing_trend
    tenant_id = request.app.state.get_effective_tenant_id(request)
    return JSONResponse({"ok": True, "data": get_sharing_trend(tenant_id, hours)})


@router.get("/api/sharing-rank")
async def api_sharing_rank(request: Request, user: dict = Depends(require_user)):
    """获取今日共享时长排行"""
    from db import get_sharing_rank
    tenant_id = request.app.state.get_effective_tenant_id(request)
    return JSONResponse({"ok": True, "data": get_sharing_rank(tenant_id)})


@router.get("/api/sharing-detail")
async def api_sharing_detail(request: Request, meeting_id: str, user_name: str = "",
                             user: dict = Depends(require_user)):
    """获取单条共享记录详情（含同会议参与人数）"""
    from db import get_sharing_detail
    tenant_id = request.app.state.get_effective_tenant_id(request)
    return JSONResponse(get_sharing_detail(meeting_id, tenant_id, user_name))


# ── Rendering helper ──────────────────────────────────────────────────────────

@router.get("/settings/bot", response_class=HTMLResponse)
async def dashboard_bot_config(request: Request, user: dict = Depends(require_user)):
    """Bot config page — redirected to settings (template removed in cleanup)."""
    from starlette.responses import RedirectResponse
    return RedirectResponse(url="/dashboard/settings#telegram", status_code=302)


@router.post("/settings/bot/verify")
async def dashboard_bot_verify(request: Request, user: dict = Depends(require_user)):
    """Verify and save a Telegram bot token for 2FA."""
    role = user.get("role", "")
    if role != "super_admin":
        raise HTTPException(status_code=403, detail="Forbidden")
    from db import set_setting
    import httpx
    try:
        form = await request.form()
        token = form.get("token", "").strip()
        if not token:
            return JSONResponse(status_code=400, content={"success": False, "error": "Token 不能为空"})
        async with httpx.AsyncClient(timeout=10) as cl:
            r = await cl.get(f"https://api.telegram.org/bot{token}/getMe")
            if r.status_code != 200:
                return JSONResponse(status_code=400, content={"success": False, "error": f"Bot Token 无效: HTTP {r.status_code}"})
            bot_info = r.json()
            if not bot_info.get("ok"):
                return JSONResponse(status_code=400, content={"success": False, "error": bot_info.get("description", "Token 无效")})
            username = bot_info.get("result", {}).get("username", "unknown")
            r2 = await cl.post(f"https://api.telegram.org/bot{token}/getUpdates", json={"limit": 1, "allowed_updates": ["message"]})
            can_read = r2.status_code == 200
            set_setting("2fa_bot_token", token)
            set_setting("2fa_bot_username", username)
            return JSONResponse(content={"success": True, "username": username, "can_read": can_read})
    except Exception as e:
        return JSONResponse(status_code=500, content={"success": False, "error": f"{type(e).__name__}: {str(e)[:200]}"})


def _render_admin(request: Request, active: str, user: dict, template_name: str,
                  **extra) -> HTMLResponse:
    """Render admin template with common context injected."""
    from fastapi.templating import Jinja2Templates
    from pathlib import Path
    templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
    from app import fmt_myt
    templates.env.globals["fmt_myt"] = fmt_myt

    # Use AuthService for template context
    auth = AuthService(request)
    ctx = auth.require_authenticated()

    # Build current_user dict matching template expectations
    current_user = {
        "id": user["id"],
        "username": user["username"],
        "display_name": user.get("display_name", ""),
        "role": user.get("role", "user"),
        "tenant_id": ctx.effective_tenant,
        "is_active": user.get("is_active_str", "true" if user.get("is_active") else "false"),
        "telegram_chat_id": user.get("telegram_chat_id", ""),
        "telegram_2fa_enabled": user.get("telegram_2fa_enabled", 0),
        "telegram_2fa_verified_at": user.get("telegram_2fa_verified_at", ""),
        "twofa_backup_codes": user.get("twofa_backup_codes", ""),
    }

    context = auth.get_template_vars(active, **extra)
    # Override request/page_title for rendering
    context["request"] = request
    context["page_title"] = extra.pop("title", "成员中心")
    # Ensure current_user from dict
    context["current_user"] = current_user

    return templates.TemplateResponse(request, template_name, context)


# ── History — Zoom 官方 Attendance CSV 上传与导入 ──

@router.get("/history", response_class=HTMLResponse)
async def dashboard_history_page(request: Request, user: dict = Depends(require_user)):
    """302 Redirect to meetings?tab=official"""
    from fastapi.responses import RedirectResponse
    return RedirectResponse(url="/dashboard/meetings?tab=official", status_code=302)

@router.post("/history/delete")
async def history_delete_csv(request: Request, user: dict = Depends(require_user)):
    """删除某个导入的 CSV 文件所有记录"""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    form = await request.form()
    source_file = (form.get("source_file") or "").strip()
    if not source_file:
        return JSONResponse({"success": False, "error": "缺少 source_file 参数"}, status_code=400)

    import db as _db
    conn = _db._get_conn()
    deleted = conn.execute(
        "DELETE FROM official_attendance_sessions WHERE tenant_id=? AND source_file=?",
        (tenant_id, source_file)
    ).rowcount
    conn.commit()
    return JSONResponse({"success": True, "deleted": deleted})


@router.post("/history/upload")
async def history_upload_csv(request: Request, user: dict = Depends(require_user)):
    import os
    import tempfile

    tenant_id = request.app.state.get_effective_tenant_id(request)

    form = await request.form()
    csv_file = form.get("file")
    if not csv_file or not hasattr(csv_file, "filename") or not csv_file.filename:
        return JSONResponse({"success": False, "error": "请选择 CSV 文件"}, status_code=400)

    if not csv_file.filename.lower().endswith(".csv"):
        return JSONResponse({"success": False, "error": "仅支持 .csv 文件"}, status_code=400)

    suffix = os.path.splitext(csv_file.filename)[1] or ".csv"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tmp:
        content = await csv_file.read()
        tmp.write(content)
        tmp_path = tmp.name

    try:
        from db import import_official_attendance_csv

        result = import_official_attendance_csv(
            tmp_path, tenant_id=tenant_id, source_file=csv_file.filename
        )
        return JSONResponse(result)
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": f"{type(e).__name__}: {str(e)[:500]}"},
            status_code=500,
        )
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


@router.get("/api/v3/history/member-sessions")
async def history_member_sessions_api(
    request: Request,
    name: str = "",
    user: dict = Depends(require_user),
):
    """返回某个成员的所有官方历史 session 明细"""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    from db import get_official_sessions_for_member
    rows = get_official_sessions_for_member(tenant_id, name, limit=1000)
    from app import fmt_myt
    def _d(dm):
        dm = dm or 0
        if dm >= 1440:
            return f"{dm//1440}d {dm%1440//60}h"
        elif dm >= 60:
            return f"{dm//60}h {dm%60}m"
        else:
            return f"{dm}m"
    return JSONResponse({
        "ok": True,
        "member": name,
        "sessions": [
            dict(
                meeting_id=r.get("meeting_id", ""),
                topic=r.get("topic", ""),
                join_time=r.get("join_time"),
                join_time_display=fmt_myt(r.get("join_time")),
                leave_time=r.get("leave_time"),
                leave_time_display=fmt_myt(r.get("leave_time")),
                duration_minutes=r.get("duration_minutes", 0),
                duration_display=_d(r.get("duration_minutes", 0)),
            )
            for r in rows
        ],
    })


@router.get("/api/v3/history/member-daily")
async def history_member_daily_api(
    request: Request,
    name: str = "",
    limit: int = 60,
    user: dict = Depends(require_user),
):
    """返回某个成员的按天汇总数据（考勤视角）"""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    from db import get_official_session_daily_summary
    result = get_official_session_daily_summary(tenant_id, name, limit=limit)
    return JSONResponse({"ok": True, **result})


@router.get("/api/v3/attendance-matrix")
async def attendance_matrix_api(
    request: Request,
    year: int = 0,
    month: int = 0,
    user: dict = Depends(require_user),
):
    """返回考勤矩阵"""
    import datetime
    now = datetime.datetime.utcnow()
    y = year or now.year
    m = month or now.month
    tenant_id = request.app.state.get_effective_tenant_id(request)
    from db import get_matrix
    result = get_matrix(tenant_id, y, m)
    return JSONResponse({"ok": True, **result})


# ── Report API 同步 ──

async def _resolve_zoom_for_tenant(tenant_id: str) -> ZoomMetrics:
    """根据 tenant_id 获取 Zoom 账号并返回 ZoomMetrics 实例
    无账号时抛 ValueError
    """
    za = db.get_zoom_account(tenant_id)
    if not za:
        raise ValueError(f"当前租户「{tenant_id}」未配置 Zoom 账号")
    logger.info(
        "[OFFICIAL_SYNC] tenant=%s zoom_account=%s host=%s",
        tenant_id, za["account_id"], za["host_email"]
    )
    return ZoomMetrics(za)


async def _sync_meeting_participants(
    zm: ZoomMetrics, tenant_id: str, meeting: dict
) -> tuple[int, int, int]:
    """同步单个会议的所有参与者到 official_attendance_sessions
    返回 (inserted, skipped, errors)
    """
    mid = str(meeting.get("id", ""))
    topic = meeting.get("topic", mid)
    host_name = meeting.get("host_name", "")
    host_email = meeting.get("host_email", "")
    meeting_start = meeting.get("start_time", "") or meeting.get("meeting_start_time", "")
    meeting_end = meeting.get("end_time", "") or meeting.get("meeting_end_time", "")
    if not meeting_end and meeting.get("duration", 0):
        from datetime import datetime, timedelta, timezone
        try:
            st = datetime.fromisoformat(meeting_start.replace("Z", "+00:00"))
            meeting_end = (st + timedelta(minutes=int(meeting["duration"]))).isoformat()
        except Exception:
            meeting_end = ""
    try:
        participants = await zm.get_report_meeting_participants(mid)
    except Exception:
        return 0, 0, 1
    inserted = skipped = errors = 0
    for p in participants:
        dur = p.get("duration", 0) or 0
        dur_min = dur // 60  # Report API 返回秒，存分钟
        try:
            rid = db.upsert_official_attendance_session(
                tenant_id=tenant_id,
                meeting_id=mid,
                topic=topic,
                host_name=host_name,
                host_email=host_email,
                meeting_start=meeting_start,
                meeting_end=meeting_end,
                participant_name=p["name"],
                email=p.get("email", ""),
                join_time=p.get("join_time", ""),
                leave_time=p.get("leave_time", ""),
                duration_minutes=float(dur_min),
            )
            if rid:
                inserted += 1
            else:
                skipped += 1
        except Exception:
            errors += 1
    return inserted, skipped, errors


@router.post("/api/v3/sync-official-yesterday")
async def sync_official_yesterday(
    request: Request,
    user: dict = Depends(require_user),
):
    """同步昨天所有会议参与者（Report API）"""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    try:
        zm = await _resolve_zoom_for_tenant(tenant_id)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    from datetime import datetime, timedelta, timezone
    now = datetime.now(timezone.utc)
    yesterday_start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    yesterday_end = yesterday_start + timedelta(days=1)
    try:
        meetings = await zm.get_past_meetings(page_size=50)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"获取会议列表失败: {e}"}, status_code=500)
    total_i = total_s = total_e = 0
    meeting_count = 0
    seen_mids = set()
    for m in meetings:
        mid = str(m.get("id", ""))
        if mid and mid in seen_mids:
            continue
        seen_mids.add(mid)
        # 过滤只同步昨天开始的会议
        st = m.get("start_time", "")
        if st:
            try:
                st_dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                if st_dt < yesterday_start or st_dt >= yesterday_end:
                    continue
            except Exception:
                pass
        i, s, e = await _sync_meeting_participants(zm, tenant_id, m)
        total_i += i
        total_s += s
        total_e += e
        meeting_count += 1
    return JSONResponse({
        "ok": True,
        "tenant": tenant_id,
        "meetings": meeting_count,
        "inserted": total_i,
        "skipped": total_s,
        "errors": total_e,
    })


@router.post("/api/v3/sync-official-month")
async def sync_official_month(
    request: Request,
    year: int = 0,
    month: int = 0,
    user: dict = Depends(require_user),
):
    """同步本月（或指定月）所有会议参与者（Report API）"""
    from datetime import datetime, timezone, timedelta
    import calendar
    tenant_id = request.app.state.get_effective_tenant_id(request)
    try:
        zm = await _resolve_zoom_for_tenant(tenant_id)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    now = datetime.now(timezone.utc)
    y = year or now.year
    m = month or now.month
    _, total_days = calendar.monthrange(y, m)
    month_start = now.replace(year=y, month=m, day=1, hour=0, minute=0, second=0, microsecond=0)
    month_end = month_start + timedelta(days=total_days)
    try:
        meetings = await zm.get_past_meetings(page_size=50, from_days=total_days + 2)
    except Exception as e:
        return JSONResponse({"ok": False, "error": f"获取会议列表失败: {e}"}, status_code=500)
    total_i = total_s = total_e = 0
    meeting_count = 0
    seen_mids = set()
    for m in meetings:
        mid = str(m.get("id", ""))
        if mid and mid in seen_mids:
            continue
        seen_mids.add(mid)
        st = m.get("start_time", "")
        if st:
            try:
                st_dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                if st_dt < month_start or st_dt >= month_end:
                    continue
            except Exception:
                pass
        i, s, e = await _sync_meeting_participants(zm, tenant_id, m)
        total_i += i
        total_s += s
        total_e += e
        meeting_count += 1
    return JSONResponse({
        "ok": True,
        "tenant": tenant_id,
        "meetings": meeting_count,
        "inserted": total_i,
        "skipped": total_s,
        "errors": total_e,
    })

# ── Identity Stability API ──────────────────────────────────────────────

@router.get("/api/member/identity-stability/{member_key}", response_class=JSONResponse)
async def api_member_identity_stability(member_key: str,
                                         days: int = 30,
                                         request: Request = None):
    """获取成员身份稳定性数据"""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    try:
        data = db.get_identity_stability(tenant_id, member_key, days)
        return JSONResponse({"ok": True, "data": data})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


@router.get("/api/member/similar/{member_key}", response_class=JSONResponse)
async def api_member_similar(member_key: str,
                              days: int = 30,
                              request: Request = None):
    """寻找相似成员"""
    tenant_id = request.app.state.get_effective_tenant_id(request)
    try:
        data = db.find_similar_members(tenant_id, member_key, days)
        return JSONResponse({"ok": True, "data": data})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)})


