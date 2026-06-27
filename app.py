import os
"""
app.py - Zoom 参会监控统一入口
Modes:
  python app.py api       → FastAPI dashboard on port 8000
  python app.py webhook   → FastAPI webhook receiver on port 9000
  python app.py monitor   → Polling service (no FastAPI deps needed)
  python app.py demo      → Demo mode (mock data, no credentials needed)
"""
import asyncio
import hashlib
import hmac
import json
import logging
import re
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import settings
import db
import telegram_push

MYT = timezone(timedelta(hours=8))
perf_logger = logging.getLogger("perf")

PERF_WARN_MS = 1000


# ── 统一获取客户端真实 IP ──

def get_client_ip(request) -> str:
    """按优先级获取客户端真实 IP:CF-Connecting-IP > X-Forwarded-For > X-Real-IP > request.client.host"""
    cf = request.headers.get("cf-connecting-ip")
    if cf:
        return cf.strip()
    xff = request.headers.get("x-forwarded-for")
    if xff:
        return xff.split(",")[0].strip()
    xri = request.headers.get("x-real-ip")
    if xri:
        return xri.strip()
    return request.client.host if request.client else "unknown"

# ── Zoom live data cache ──
_zoom_live_cache: dict = {}
_zoom_live_cache_time: dict = {}
ZOOM_LIVE_CACHE_TTL = 120  # seconds (was 30)
ZOOM_LIVE_FALLBACK_TTL = 240  # use stale cache up to 240s if Zoom API fails (was 90)

def _get_cached_zoom_live(tid: str) -> dict | None:
    cached = _zoom_live_cache.get(tid)
    cached_at = _zoom_live_cache_time.get(tid, 0)
    now = time.monotonic()
    if cached is None:
        return None
    if now - cached_at < ZOOM_LIVE_CACHE_TTL:
        return cached
    # expired but usable as fallback
    if now - cached_at < ZOOM_LIVE_FALLBACK_TTL:
        return cached
    return None

def _set_cached_zoom_live(tid: str, data: dict) -> None:
    _zoom_live_cache[tid] = data
    _zoom_live_cache_time[tid] = time.monotonic()


def _t_ms() -> float:
    return time.monotonic() * 1000

def _log_perf(label: str, ms: float) -> None:
    if ms >= PERF_WARN_MS:
        perf_logger.warning("[PERF] %s took %.0fms", label, ms)
    else:
        perf_logger.info("[PERF] %s %.0fms", label, ms)

def to_myt(dt_str):
    """将 UTC 时间字符串转为北京时间显示"""
    if not dt_str:
        return ""
    try:
        s = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone(MYT).strftime("%m-%d %H:%M:%S")
    except:
        return dt_str[:16]


def to_myt_display(dt_str, fmt="%m-%d %H:%M:%S"):
    """统一 MYT 转换函数"""
    if not dt_str:
        return ""
    try:
        s = dt_str.replace("Z", "+00:00")
        if "+" not in s and s.count("-") >= 2:
            # naive datetime, assume UTC
            s += "+00:00"
        dt = datetime.fromisoformat(s)
        return dt.astimezone(MYT).strftime(fmt)
    except:
        return dt_str[:16]


# ── 统一时间工具函数 ──────────────────────────────────────────────────
# 符合 TIME_TARGET_STATE.md 规范

def utc_now() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


def myt_now() -> datetime:
    """当前 MYT 时间"""
    return datetime.now(timezone.utc).astimezone(MYT)


def myt_day_range_to_utc(dt: datetime = None) -> tuple[str, str]:
    """MYT 某日的 UTC 起止 ISO 字符串"""
    if dt is None:
        dt = myt_now()
    myt_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    myt_end = myt_start + timedelta(days=1)
    return (myt_start.astimezone(timezone.utc).isoformat(),
            myt_end.astimezone(timezone.utc).isoformat())


def parse_utc_iso(s: str) -> datetime | None:
    """安全解析 ISO 时间字符串为 UTC aware datetime"""
    if not s:
        return None
    try:
        s2 = s.replace("Z", "+00:00")
        # 如果仍无时区，默认 UTC
        if "+" not in s2 and s2.count("-") <= 2:  # "2026-06-13 17:10:37" 只有日期之间的横线
            s2 = s2 + "+00:00"
        return datetime.fromisoformat(s2)
    except:
        return None


def iso_to_myt_str(s: str, fmt: str = "%m-%d %H:%M:%S") -> str:
    """UTC ISO → MYT 显示字符串"""
    if not s:
        return "-"
    dt = parse_utc_iso(s)
    if dt is None:
        return s[:16] if s else "-"
    return dt.astimezone(MYT).strftime(fmt)


# ── 智能 MYT 格式化 ──────────────────────────────────────────────────

WEEKDAY_CN = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]

def fmt_myt(dt_str: str | None) -> str:
    """统一 MYT 格式:MM-DD HH:mm:ss"""
    return iso_to_myt_str(dt_str)



def dedup_participants(participants):
    """合并连续同人的进出记录，只保留状态变化"""
    if not participants:
        return []
    result = [participants[0]]
    for p in participants[1:]:
        p_name = p.get('name', '') if isinstance(p, dict) else ''
        p_act = p.get('action', '') if isinstance(p, dict) else ''
        last = result[-1]
        l_name = last.get('name', '') if isinstance(last, dict) else ''
        l_act = last.get('action', '') if isinstance(last, dict) else ''
        if p_name != l_name or p_act != l_act:
            result.append(p)
    return result

BASE_DIR = Path(__file__).parent

with open(BASE_DIR / "brand.json") as _f:
    BRAND = json.load(_f)

# ── FastAPI app (lazy init, only for api/webhook modes) ─────────────────────
_app = None
DB_INITED = False

# ── Demo 数据函数(惰性导入)────────────────────────────────────────────────
_DEMO_MODULE = None


def _ensure_demo():
    global _DEMO_MODULE
    if _DEMO_MODULE is None:
        import importlib
        _DEMO_MODULE = importlib.import_module("demo_data")
    return _DEMO_MODULE


def resolve_member(raw_name: str, tenant_id: str = None) -> dict:
    """解析原始 Zoom 用户名:返回标准名、分组和是否经过映射"""
    resolved = db.resolve_display_name(raw_name)
    standard = resolved["display_name"]
    group_name = db.get_member_group(standard, tenant_id) or db.get_member_group(raw_name, tenant_id)
    is_mapped = standard != raw_name
    return {"raw_name": raw_name, "standard_name": standard, "group_name": group_name, "is_mapped": is_mapped}


def _validate_group_tenant(group_id: int | None, member_tenant_id: str | None) -> None:
    """校验 group_id 的租户归属，跨租户则抛 ValueError"""
    if group_id is None or not member_tenant_id:
        return
    from db import _get_conn
    row = _get_conn().execute(
        "SELECT tenant_id, name FROM member_groups WHERE id = ?", (group_id,)
    ).fetchone()
    if row is None:
        # 分组可能已被删除，允许清理但记录
        return
    if row["tenant_id"] != member_tenant_id:
        raise ValueError(
            f"跨租户分组绑定被拒绝: "
            f"group '{row['name']}' (tenant={row['tenant_id']}) "
            f"!= member_tenant={member_tenant_id}"
        )


def build_app() -> "FastAPI":
    """创建并配置完整的 FastAPI 应用(只在 api/webhook 模式下调用)"""
    global _app
    if _app is not None:
        return _app

    from fastapi import FastAPI, Request, HTTPException, Form
    from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse, FileResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    app = FastAPI(title=BRAND["app_name_zh"], version="2.0.0")

    # ── Favicon ────────────────────────────────────────────────────────────
    @app.get("/favicon.ico", response_class=FileResponse)
    async def favicon():
        return "static/favicon.ico"

    # ── 全局 state 初始化 ────────────────────────────────────────────────────
    app.state._2fa_pending = {}

    # ── 认证中间件(必须注册在 SessionMiddleware 之前，使其成为 innermost) ─────
    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        """全局认证中间件 - 未登录用户重定向到 /login"""
        path = request.url.path

        # 白名单:这些路径不需要认证
        public_paths = [
            "/login", "/logout",
            "/privacy", "/terms",
            "/static/",
            "/favicon.ico",
            "/api/v2/auth/",
            "/api/v3/live",
            "/api/v3/telegram-rules/",
            "/api/health-check",
            "/webhook",
            "/dashboard/tenant/webhook/",
            "/health",
        ]
        if any(path.startswith(pp) for pp in public_paths):
            return await call_next(request)

        user_id = request.session.get("user_id")
        if not user_id:
            return RedirectResponse(url="/login", status_code=302)

        user = db.get_user_by_id(user_id)
        if not user:
            request.session.clear()
            return RedirectResponse(url="/login", status_code=302)

        return await call_next(request)

    try:
        from starlette.middleware.sessions import SessionMiddleware
        app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, max_age=86400, same_site="lax", https_only=True)
    except ImportError:
        pass
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    tmpl = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    tmpl.env.globals["to_myt"] = to_myt
    tmpl.env.globals["fmt_myt"] = fmt_myt
    tmpl.env.filters["myt"] = to_myt

    # ── DB 初始化中间件 ─────────────────────────────────────────────────────
    # ── Alert rules seed ────────────────────────────────────────────────
    try:
        conn = db._get_conn()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        # sharing_timeout rule: enabled by default, threshold 30 min
        conn.execute(
            "INSERT OR IGNORE INTO alert_rules (rule_type, enabled, threshold_minutes, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("sharing_timeout", 1, 30, now, now)
        )
        conn.execute(
            "INSERT OR IGNORE INTO alert_rules (rule_type, enabled, created_at, updated_at) VALUES (?, ?, ?, ?)",
            ("webhook_event_push", 1, now, now)
        )
        conn.commit()
    except:
        pass
    # ────────────────────────────────────────────────────────────────────


    @app.middleware("http")
    async def _ensure_db(request: Request, call_next):
        global DB_INITED
        if not DB_INITED:
            if settings.demo_mode:
                _ensure_demo().seed_demo_data()
            else:
                db.init_db()
            DB_INITED = True
        response = await call_next(request)
        # 强制不缓存所有页面(避免 Cloudflare/CDN 缓存 stale HTML)
        response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate, proxy-revalidate"
        response.headers["Pragma"] = "no-cache"
        response.headers["Expires"] = "0"
        return response

    # ── 政策页面(用于 Zoom Marketplace 审核) ────────────────────────────────
    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy_page():
        return HTMLResponse("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Privacy Policy - Zoom Attendance Monitor</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.7;color:#334;background:#fafafa}h1{color:#1a1a2e;border-bottom:2px solid #e2e8f0;padding-bottom:12px}h2{color:#1a1a2e;margin-top:32px}p{color:#475569}footer{margin-top:48px;font-size:14px;color:#94a3b8}</style></head><body>
<h1>Privacy Policy</h1>
<p><strong>Last updated:</strong> June 2026</p>
<h2>Information We Collect</h2>
<p>Zoom Attendance Monitor collects the following data from authorized Zoom accounts:</p>
<ul><li>Meeting metadata (ID, topic, start/end time)</li><li>Participant names and join/leave timestamps</li><li>Screen sharing activity indicators</li></ul>
<h2>How We Use Information</h2>
<p>All collected data is used solely to generate attendance reports and real-time monitoring dashboards as requested by the account owner. No data is sold, shared, or used for any other purpose.</p>
<h2>Data Storage</h2>
<p>Data is stored on a private server and retained for the duration of the service subscription. Account owners may request deletion of their data at any time by contacting support.</p>
<h2>Data Sharing</h2>
<p>We do not share personal data with third parties. Data is accessible only to the account owner who authorized the app and their designated team members.</p>
<h2>Contact</h2>
<p>For privacy inquiries or data deletion requests: support@dhbwang.xyz</p>
<footer>Zoom Attendance Monitor - dhbwang.xyz</footer>
</body></html>""")

    @app.get("/terms", response_class=HTMLResponse)
    async def terms_page():
        return HTMLResponse("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Terms of Service - Zoom Attendance Monitor</title>
<style>body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;max-width:720px;margin:40px auto;padding:0 20px;line-height:1.7;color:#334;background:#fafafa}h1{color:#1a1a2e;border-bottom:2px solid #e2e8f0;padding-bottom:12px}h2{color:#1a1a2e;margin-top:32px}p{color:#475569}footer{margin-top:48px;font-size:14px;color:#94a3b8}</style></head><body>
<h1>Terms of Service</h1>
<p><strong>Last updated:</strong> June 2026</p>
<h2>Acceptance</h2>
<p>By authorizing Zoom Attendance Monitor, you agree to these terms. If you do not agree, do not use the service.</p>
<h2>Service Description</h2>
<p>Zoom Attendance Monitor provides meeting attendance tracking and reporting for authorized Zoom accounts. The service requires read-only access to meeting and participant data.</p>
<h2>User Responsibilities</h2>
<ul><li>You must have the authority to grant access to the Zoom account</li><li>You are responsible for configuring monitoring preferences</li><li>You must comply with applicable privacy laws when monitoring meetings</li></ul>
<h2>Limitation of Liability</h2>
<p>The service is provided "as is" without warranty. We are not liable for any damages arising from use of the service.</p>
<h2>Termination</h2>
<p>You may revoke access at any time via Zoom Marketplace. We may terminate service for violation of these terms.</p>
<h2>Contact</h2>
<p>support@dhbwang.xyz</p>
<footer>Zoom Attendance Monitor - dhbwang.xyz</footer>
</body></html>""")

    # ── 多租户 ZoomMetrics 辅助函数 ──────────────────────────────────────────
    def _get_tenant_zoom_metrics(request: Request) -> tuple:
        """根据当前会话的 tenant_id 获取对应的 ZoomMetrics 实例。

        规则:严格数据隔离。
        - 当前租户有活跃 zoom_account → 用它的(自有 S2S 专属视图)
        - 没有 → 返回 None(不跨租户 fallback)
        - 无 tenant 上下文 → 用全局 .env

        Returns:
            (ZoomMetrics | None, tenant_id | None)
        """
        from zoom_metrics import ZoomMetrics

        tenant_id = request.app.state.get_effective_tenant_id(request)

        # 1) 当前 tenant 的 zoom_account(如果有，就是专属视图)
        if tenant_id:
            accounts = db.get_zoom_accounts(tenant_id)
            active = next(
                (a for a in accounts if a.get("is_active") and a.get("status") == "active"),
                None,
            )
            if active:
                return ZoomMetrics(active), tenant_id

        # 2) 没有自己的账号 → 无数据(不跨租户 fallback，严格隔离)
        if tenant_id:
            return None, tenant_id

        # 3) 无 tenant 上下文(未登录)→ 用 default 账号
        za = db.get_zoom_account("default")
        if za:
            return ZoomMetrics(za), None
        return None, None

    # ── 看板 ─────────────────────────────────────────────────────────────────
    def _compute_online_from_webhook(tid: str) -> tuple:
        """Reconstruct current online count & active meetings from zoom_participants.
           Returns (online_count: int, meetings: list[dict]).
           Logic: for each meeting_id, if the latest action for a user is 'enter', they're online.
           Meeting is active if it has at least one online user.
        """
        conn = db._get_conn()
        # Get all enter/leave records in the last 2h for this tenant, ordered per meeting per name
        rows = conn.execute(
            "SELECT meeting_id, name, action, action_time "
            "FROM zoom_participants WHERE tenant_id=? AND action_time >= datetime('now', '-2 hours') "
            "ORDER BY meeting_id, name, action_time DESC",
            (tid,),
        ).fetchall()
        online_map = {}  # meeting_id -> set of online names
        meetings_seen = {}  # meeting_id -> first seen time
        for r in rows:
            mid = r["meeting_id"]
            name = r["name"]
            action = r["action"]
            if mid not in online_map:
                online_map[mid] = set()
                meetings_seen[mid] = r["action_time"]
            if action == "enter":
                online_map[mid].add(name)
            elif action == "leave":
                online_map[mid].discard(name)
        total_online = sum(len(names) for names in online_map.values())
        meetings = [
            {"id": mid, "topic": f"Meeting {mid[-6:]}", "participant_count": len(names), "start_time": meetings_seen.get(mid, "")}
            for mid, names in sorted(online_map.items(), key=lambda x: -len(x[1]))
            if names  # only meetings with active participants
        ]
        return total_online, meetings

    @app.get("/", response_class=RedirectResponse)
    async def landing(request: Request):
        """Landing Page - 重定向到数据看板"""
        return RedirectResponse(url="/dashboard")

    async def _compute_kpi_data(tid: str) -> dict:
        """Compute KPI data for tenant dashboard - all queries tenant-isolated.
           Uses Webhook reconstruction as base, Metrics API as Business enhancement."""
        t_kpi = _t_ms()
        t0 = _t_ms()
        today_participants = len(dedup_participants(db.get_today_participants(limit=10000, tenant_id=tid)))
        _log_perf("kpi_today_participants", _t_ms() - t0)
        t_db = _t_ms()
        
        # ── Online - Single Source of Truth ──
        try:
            if '/app/data' not in sys.path:
                sys.path.insert(0, '/app/data')
            import service
            online_data = await asyncio.wait_for(
                service.get_online_state(tid), timeout=5.0
            )
            current_online = online_data["online_count"]
            active_meetings = online_data["active_meetings"]
            _log_perf("kpi_service_online", _t_ms() - t_db)
        except Exception:
            # Fallback to old logic
            online_data = db.get_current_online(tid)
            current_online = online_data["online_count"]
            active_meetings = online_data["active_meetings"]

        # ── No longer call per-tenant Metrics API separately — service.get_online_state already handles it
        conn = db._get_conn()
        today_myt = datetime.now(MYT).strftime("%Y-%m-%d")
        myt_day_start_utc = datetime.now(MYT).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM zoom_events WHERE created_at >= ? AND tenant_id = ?",
            (myt_day_start_utc, tid),
        ).fetchone()
        today_events = row["c"] if row else 0
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM alerts WHERE tenant_id = ? AND created_at >= ? AND alert_type NOT IN ('webhook_push')",
            (tid, myt_day_start_utc),
        ).fetchone()
        today_alerts = row["c"] if row else 0
        _log_perf("kpi_db_query", _t_ms() - t_db)
        t_events = _t_ms()
        recent_events = db.get_recent_events(limit=5, tenant_id=tid)
        recent_alerts = db.get_recent_alerts(limit=5, tenant_id=tid)
        _log_perf("kpi_events", _t_ms() - t_events)
        t_push = _t_ms()
        channels = db.get_tenant_channels(tid)
        push_configured = any(c.get("is_enabled") for c in channels)
        push_channel_count = len([c for c in channels if c.get("is_enabled")])
        _log_perf("kpi_push", _t_ms() - t_push)
        t_part = _t_ms()
        participants = dedup_participants(db.get_today_participants(limit=200, tenant_id=tid))
        # ── 按 name 去重，取每个人最新一条(dashboard 最近活跃成员用) ──
        seen = {}
        for p in participants:
            name = p.get("name") or p.get("user_name", "")
            if name and name not in seen:
                seen[name] = p
        participants_deduped = list(seen.values())
        _log_perf("kpi_participants_dedup", _t_ms() - t_part)
        _log_perf("kpi_total", _t_ms() - t_kpi)
        return {
            "today_participants": today_participants,
            "current_online": current_online,
            "today_events": today_events,
            "today_alerts": today_alerts,
            "active_meetings": active_meetings,
            "recent_events": recent_events,
            "push_configured": push_configured,
            "push_channel_count": push_channel_count,
            "participants": participants_deduped,
        }

    async def _compute_setup_status(tid: str) -> dict:
        """Compute setup readiness score and checks for a tenant."""
        checks = {}
        accounts = db.get_zoom_accounts(tid)
        has_account = any(a.get("is_active") and a.get("client_id") for a in accounts)
        checks["zoom_account"] = has_account
        has_oauth = any(
            a.get("is_active") and a.get("status") == "active"
            for a in accounts
        )
        checks["oauth"] = bool(has_oauth)
        # --- meetings check: use webhook as base, Metrics as enhancement ---
        conn_setup = db._get_conn()
        rows_setup = conn_setup.execute(
            "SELECT COUNT(DISTINCT meeting_id) AS c FROM zoom_participants WHERE tenant_id=? AND action='enter' AND action_time >= datetime('now', '-2 hours')",
            (tid,),
        ).fetchone()
        has_active_meetings = (rows_setup["c"] if rows_setup else 0) > 0
        _tenant_info = db.get_tenant(tid)
        if _tenant_info and _tenant_info.get("metrics_available", 0):
            try:
                accounts_db_setup = db.get_zoom_accounts(tid)
                active_setup = next(
                    (a for a in accounts_db_setup if a.get("is_active") and a.get("status") == "active"),
                    None,
                )
                if active_setup:
                    from zoom_metrics import ZoomMetrics
                    zm = ZoomMetrics(active_setup)
                    live_data_setup = await zm.get_live()
                    meetings_setup = live_data_setup.get("meetings", [])
                    if meetings_setup and any(m.get("participants") for m in meetings_setup):
                        has_active_meetings = True
            except Exception:
                pass
        checks["meetings"] = has_active_meetings
        conn = db._get_conn()
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM zoom_participants WHERE tenant_id = ?",
            (tid,),
        ).fetchone()
        participant_count = row["c"] if row else 0
        checks["participants"] = participant_count > 0
        from datetime import datetime, timedelta, timezone
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%d %H:%M:%S")
        row = conn.execute(
            "SELECT COUNT(*) AS c FROM zoom_events WHERE created_at >= ? AND tenant_id = ?",
            (cutoff, tid),
        ).fetchone()
        webhook_count = row["c"] if row else 0
        checks["webhook"] = webhook_count > 0
        channels = db.get_tenant_channels(tid)
        has_telegram = any(c.get("is_enabled") for c in channels)
        checks["telegram"] = bool(has_telegram)
        all_rows = conn.execute(
            "SELECT COUNT(*) AS c FROM member_display WHERE tenant_id = ?",
            (tid,),
        ).fetchone()
        unmapped = conn.execute(
            "SELECT COUNT(*) AS c FROM member_display "
            "WHERE tenant_id = ? AND display_name = raw_name",
            (tid,),
        ).fetchone()
        total_members = all_rows["c"] if all_rows else 0
        unmapped_count = unmapped["c"] if unmapped else 0
        if total_members > 0:
            mapped_rate = (total_members - unmapped_count) / total_members
        else:
            mapped_rate = 0
        checks["member_mapping"] = round(mapped_rate, 2)
        dup_rows = conn.execute(
            "SELECT display_name, COUNT(*) as c FROM member_display "
            "WHERE tenant_id = ? GROUP BY display_name HAVING c > 1",
            (tid,),
        ).fetchall()
        checks["duplicates"] = len(dup_rows)
        weights = {
            "zoom_account": 20, "oauth": 15, "meetings": 10,
            "participants": 15, "webhook": 15, "telegram": 10,
            "member_mapping": 10, "duplicates": 5,
        }
        score = 0
        for key, weight in weights.items():
            if key == "member_mapping" and isinstance(checks[key], (int, float)):
                score += int(weight * checks[key])
            elif key == "duplicates" and checks[key] == 0:
                score += weight
            elif isinstance(checks[key], bool) and checks[key]:
                score += weight
        score = min(score, 100)
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
            next_steps.append("配置 Webhook(接收实时事件)")
        if not checks["telegram"]:
            next_steps.append("创建推送频道")
        if isinstance(checks["member_mapping"], (int, float)) and checks["member_mapping"] < 0.8:
            next_steps.append("完成成员映射(当前 {}%)".format(int(checks["member_mapping"] * 100)))
        return {
            "score": score,
            "status": status_label,
            "checks": checks,
            "next_steps": next_steps,
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        # Redirect to admin_router's dashboard - the real handler lives there.
        from starlette.responses import RedirectResponse
        target = request.url_for("dashboard_index")
        return RedirectResponse(url=str(target), status_code=302)

    # ── Demo ──────────────────────────────────────────────────────────────────
    @app.get("/demo", response_class=HTMLResponse)
    async def demo_page(request: Request, tab: str = "overview"):
        """Demo 模式 - 免 Zoom 账号完整体验"""
        demo = _ensure_demo()
        demo.seed_demo_data()
        stats = demo.get_demo_stats()
        alerts = demo.get_demo_alerts()
        participants = demo.get_demo_participants()
        events = demo.get_demo_events()
        reports = demo.get_demo_reports()
        analytics = demo.get_demo_analytics()
        return tmpl.TemplateResponse(request, "demo.html", {
            "brand": BRAND,
            "stats": stats,
            "alerts": alerts,
            "participants": participants,
            "events": events,
            "reports": reports,
            "analytics": analytics,
            "active_tab": tab,
            "demo_mode": True,
        })

    @app.get("/api/demo/reset")
    async def demo_reset():
        """重置 demo 数据并重新 seed"""
        demo = _ensure_demo()
        demo.reset_demo()
        demo.seed_demo_data()
        return demo.get_demo_stats()

    # ── Dashboard JSON API (JS polling) ───────────────────────────────────────
    # In-memory cache for Zoom live data: key=tenant_id, value=cached result
    _dash_cache: dict = {}
    _dash_cache_time: dict = {}
    DASH_CACHE_TTL = 30  # seconds

    @app.get("/dashboard/data")
    async def dashboard_data_api(request: Request):
        """JSON endpoint for dashboard JS polling - tenant-scoped."""
        t_total = time.monotonic()

        if settings.demo_mode:
            ...

        tid = request.app.state.get_effective_tenant_id(request)
        now = time.monotonic()

        # ── KPI 级别缓存 ──
        cached = _dash_cache.get(tid)
        if cached and (now - _dash_cache_time.get(tid, 0)) < DASH_CACHE_TTL:
            kpi = cached
        else:
            kpi = await _compute_kpi_data(tid)
            _dash_cache[tid] = kpi
            _dash_cache_time[tid] = now

        _log_perf("dashboard_total", (time.monotonic() - t_total) * 1000)
        return {
            "kpi": {
                "today_participants": kpi["today_participants"],
                "online_now": kpi["current_online"],
                "today_events": kpi["today_events"],
                "today_alerts": kpi["today_alerts"],
            },
            "active_meetings": kpi["active_meetings"],
            "recent_events": kpi["recent_events"],
            "participants": kpi["participants"],
        }

    # ── 生产数据页面 ───────────────────────────────────────────────────────────
    @app.get("/events", response_class=HTMLResponse)
    async def events_page(request: Request):
        if settings.demo_mode:
            events = _ensure_demo().get_demo_events()
        else:
            events = db.get_recent_events(limit=100, tenant_id=request.app.state.get_effective_tenant_id(request))
        return tmpl.TemplateResponse(request, "events.html", {
            "events": events,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
        })

    @app.get("/participants", response_class=HTMLResponse)
    async def participants_page(request: Request):
        """统一重定向到 /dashboard/participants。所有成员管理都在 dashboard 路径下。"""
        role_val = request.session.get("role")
        user_id = request.session.get("user_id")
        if not user_id or not role_val:
            return RedirectResponse(url="/login")
        return RedirectResponse(url="/dashboard/participants")

    @app.get("/alerts", response_class=HTMLResponse)
    async def alerts_page(request: Request):
        if settings.demo_mode:
            alerts = _ensure_demo().get_demo_alerts()
        else:
            alerts = db.get_recent_alerts(limit=100, tenant_id=request.app.state.get_effective_tenant_id(request))
        return tmpl.TemplateResponse(request, "alerts.html", {
            "alerts": alerts,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
        })

    @app.get("/reports", response_class=HTMLResponse)
    async def reports_page(request: Request):
        if settings.demo_mode:
            reports = _ensure_demo().get_demo_reports()
        else:
            conn = db._get_conn()
            tenant_id = request.app.state.get_effective_tenant_id(request)
            # 今日统计(MYT 边界)
            rs_utc, re_utc = myt_day_range_to_utc()
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if tenant_id:
                total_today = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND tenant_id=?", (rs_utc, re_utc, tenant_id)).fetchone()[0]
                unique_names = conn.execute("SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND tenant_id=?", (rs_utc, re_utc, tenant_id)).fetchone()[0]
                rows = []
                for i in range(7, -1, -1):
                    d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
                    cnt = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND tenant_id=?", (d, (datetime.fromisoformat(d) + timedelta(days=1)).isoformat(), tenant_id)).fetchone()[0]
                    uniq = conn.execute("SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND tenant_id=?", (d, (datetime.fromisoformat(d) + timedelta(days=1)).isoformat(), tenant_id)).fetchone()[0]
                    rows.append({"date": d, "count": cnt, "unique": uniq})
                top = conn.execute("SELECT name, COUNT(*) as cnt FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND tenant_id=? GROUP BY name ORDER BY cnt DESC LIMIT 10", (rs_utc, re_utc, tenant_id)).fetchall()
            else:
                total_today = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (rs_utc, re_utc)).fetchone()[0]
                unique_names = conn.execute("SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (rs_utc, re_utc)).fetchone()[0]
                rows = []
                for i in range(7, -1, -1):
                    d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
                    cnt = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (d, (datetime.fromisoformat(d) + timedelta(days=1)).isoformat())).fetchone()[0]
                    uniq = conn.execute("SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (d, (datetime.fromisoformat(d) + timedelta(days=1)).isoformat())).fetchone()[0]
                    rows.append({"date": d, "count": cnt, "unique": uniq})
                top = conn.execute("SELECT name, COUNT(*) as cnt FROM zoom_participants WHERE action_time >= ? AND action_time < ? GROUP BY name ORDER BY cnt DESC LIMIT 10", (rs_utc, re_utc)).fetchall()
            reports = {"total_today": total_today, "unique_today": unique_names, "daily_trend": rows, "top_participants": [{"name": r[0], "count": r[1]} for r in top]}
        return tmpl.TemplateResponse(request, "reports.html", {
            "reports": reports,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
        })

    @app.get("/analytics", response_class=HTMLResponse)
    async def analytics_page(request: Request):
        if settings.demo_mode:
            analytics = _ensure_demo().get_demo_analytics()
        else:
            analytics = {}
        return tmpl.TemplateResponse(request, "analytics.html", {
            "analytics": analytics,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
        })

    @app.get("/summary", response_class=HTMLResponse)
    async def summary_page(request: Request):
        return tmpl.TemplateResponse(request, "summary.html", {"brand": BRAND})

    @app.get("/live", response_class=HTMLResponse)
    async def live_page(request: Request):
        return tmpl.TemplateResponse(request, "live.html", {"brand": BRAND})

    @app.get("/attendance", response_class=HTMLResponse)
    async def attendance_page(request: Request):
        return tmpl.TemplateResponse(request, "attendance.html", {"brand": BRAND})

    # ── 设置 ────────────────────────────────────────────────────────────────
    # 旧路由重定向(向后兼容)
    @app.get("/settings/zoom", response_class=HTMLResponse)
    @app.get("/settings/telegram", response_class=HTMLResponse)
    async def settings_old_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/settings/system")

    # ── Telegram Rules API ──────────────────────────────────────────────

    @app.get("/api/v3/telegram-rules")
    async def api_v3_get_telegram_rules(request: Request):
        """获取所有预警规则（按租户隔离，走 AlertService）"""
        tenant_id = request.app.state.get_effective_tenant_id(request) if hasattr(request.app.state, 'get_effective_tenant_id') else (request.session.get("selected_tenant") or request.session.get("tenant_id") or "default")
        from services.alert import AlertService
        rules = AlertService().get_rules(tenant_id)
        return {"ok": True, "rules": rules}

    @app.post("/api/v3/telegram-rules")
    async def api_v3_create_telegram_rule(request: Request):
        """创建预警规则（按租户隔离，走 AlertService）"""
        data = await request.json()
        event_type = data.get("event_type", "").strip()
        tenant_id = request.app.state.get_effective_tenant_id(request) if hasattr(request.app.state, 'get_effective_tenant_id') else (request.session.get("selected_tenant") or request.session.get("tenant_id") or "default")
        if not event_type:
            return {"ok": False, "error": "event_type is required"}
        from services.alert import AlertService
        rule_id = AlertService().upsert_rule(tenant_id, event_type, data)
        return {"ok": True, "id": rule_id}

    @app.put("/api/v3/telegram-rules/{event_type}")
    async def api_v3_update_telegram_rule(event_type: str, request: Request):
        """更新预警规则（按租户隔离，走 AlertService）"""
        data = await request.json()
        tenant_id = request.app.state.get_effective_tenant_id(request) if hasattr(request.app.state, 'get_effective_tenant_id') else (request.session.get("selected_tenant") or request.session.get("tenant_id") or "default")
        from services.alert import AlertService
        rule_id = AlertService().upsert_rule(tenant_id, event_type, data)
        return {"ok": True, "id": rule_id}

    @app.delete("/api/v3/telegram-rules/{event_type}")
    async def api_v3_delete_telegram_rule(event_type: str, request: Request):
        """删除预警规则（按租户隔离，走 AlertService）"""
        tenant_id = request.app.state.get_effective_tenant_id(request) if hasattr(request.app.state, 'get_effective_tenant_id') else (request.session.get("selected_tenant") or request.session.get("tenant_id") or "default")
        from services.alert import AlertService
        AlertService().delete_rule(tenant_id, event_type)
        return {"ok": True}

    @app.post("/api/v3/telegram-rules/{event_type}/test")
    async def api_v3_test_telegram_rule(event_type: str, request: Request):
        """测试预警规则推送（走 AlertService）"""
        import traceback
        try:
            tenant_id = request.app.state.get_effective_tenant_id(request) if hasattr(request.app.state, "get_effective_tenant_id") else (request.session.get("selected_tenant") or request.session.get("tenant_id") or "default")
            from services.alert import AlertService
            result = AlertService().test_rule(tenant_id, event_type)
            if not result.get("ok"):
                status_code = 404 if "不存在" in (result.get("error") or "") else 400
                return JSONResponse({"ok": False, "error": result.get("error")}, status_code=status_code)
            db.update_rule_test_result(tenant_id, event_type, True)
            return {"ok": True, "message": "测试消息已发送"}
        except Exception as e:
            tb = traceback.format_exc()
            print(f"[TEST-RULE-ERROR] {event_type}: {e}\n{tb}", flush=True)
            return JSONResponse({"ok": False, "error": str(e)}, status_code=500)

    @app.get("/api/v3/telegram-rules/discover")
    async def api_v3_discover_telegram_rules():
        conn = db._get_conn()
        # 从 zoom_events 查出所有不同的 event_type
        rows = conn.execute(
            "SELECT DISTINCT event_type FROM zoom_events ORDER BY event_type"
        ).fetchall()
        # 排除已在 telegram_alert_rules 中的
        existing_rows = conn.execute(
            "SELECT event_type FROM telegram_alert_rules"
        ).fetchall()
        existing = set(r[0] for r in existing_rows)

        title_map = {
            "meeting.participant_admitted": "准入",
            "meeting.started": "开始",
            "meeting.ended": "结束",
            "meeting.participant_jbh_waiting": "等待中",
            "meeting.participant_jbh_waiting_left": "离开等待",
            "meeting.participant_joined": "加入",
            "meeting.participant_joined_breakout_room": "加入分组",
            "meeting.participant_joined_waiting_room": "等候室",
            "meeting.participant_left": "离开",
            "meeting.participant_left_breakout_room": "离开分组",
            "meeting.participant_left_waiting_room": "离开等候室",
            "meeting.sharing_started": "共享开始",
            "meeting.sharing_ended": "共享结束",
            "meeting.breakout_room_sharing_started": "分组共享",
            "meeting.breakout_room_sharing_ended": "分组共享停",
            "user.presence_status_updated": "状态",
        }

        discovered = []
        for (event_type,) in rows:
            if event_type in existing:
                continue
            # 排除测试事件
            if event_type in ("concurrent_test",) or event_type.startswith("test.") or event_type.startswith("test_"):
                continue
            title = title_map.get(event_type, event_type.rsplit(".", 1)[-1])
            discovered.append({"event_type": event_type, "title": title})

        return {"ok": True, "discovered": discovered}

    # ── Member Groups API ─────────────────────────────────────────────────

    @app.get("/api/v3/member-groups")
    async def api_v3_get_member_groups(request: Request):
        """获取当前租户的所有成员分组"""
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.group import GroupService
        gs = GroupService()
        groups = gs.get_all_groups(tenant_id)
        return {"ok": True, "groups": groups}

    @app.post("/api/v3/member-groups")
    async def api_v3_create_member_group(request: Request):
        """创建成员分组--super_admin 可在任意租户创建"""
        role = request.session.get("role", "tenant")
        if role not in ("super_admin", "tenant_admin", "owner"):
            return JSONResponse(status_code=403, content={"ok": False, "error": "无权限"})
        data = await request.json()
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.group import GroupService
        gs = GroupService()
        gid = gs.create_group(name, description, tenant_id)
        return {"ok": True, "id": gid}

    @app.put("/api/v3/member-groups/{group_id}")
    async def api_v3_update_member_group(group_id: int, request: Request):
        """更新成员分组"""
        role = request.session.get("role", "tenant")
        if role not in ("super_admin", "tenant_admin", "owner"):
            return JSONResponse(status_code=403, content={"ok": False, "error": "无权限"})
        data = await request.json()
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.group import GroupService
        gs = GroupService()
        ok = gs.update_group(group_id, name, description, tenant_id)
        return {"ok": ok}

    @app.delete("/api/v3/member-groups/{group_id}")
    async def api_v3_delete_member_group(group_id: int, request: Request):
        """删除成员分组（仅限租户内操作）"""
        role = request.session.get("role", "tenant")
        if role not in ("super_admin", "tenant_admin", "owner"):
            return JSONResponse(status_code=403, content={"ok": False, "error": "无权限"})
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.group import GroupService
        gs = GroupService()
        ok = gs.delete_group(group_id, tenant_id)
        return {"ok": ok}

    @app.post("/api/v3/member-groups/{group_id}/members")
    async def api_v3_add_member(group_id: int, request: Request):
        """向分组添加成员"""
        data = await request.json()
        member_name = data.get("member_name", "").strip()
        if not member_name:
            return {"ok": False, "error": "member_name is required"}
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.group import GroupService
        gs = GroupService()
        ok = gs.add_member(group_id, member_name, tenant_id)
        return {"ok": ok}

    @app.delete("/api/v3/member-groups/{group_id}/members/{member_name}")
    async def api_v3_remove_member(group_id: int, member_name: str, request: Request):
        """从分组移除成员（仅限租户内操作）"""
        import urllib.parse
        member_name = urllib.parse.unquote(member_name)
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.group import GroupService
        gs = GroupService()
        ok = gs.remove_member(group_id, member_name, tenant_id)
        return {"ok": ok}

    @app.get("/settings/member-groups", response_class=HTMLResponse)
    async def settings_member_groups_page(request: Request):
        """成员分组配置页面"""
        groups = db.get_all_groups()
        rendered = tmpl.TemplateResponse(request, "settings_member_groups.html", {
            "brand": BRAND,
            "groups": groups,
        })
        rendered.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        return rendered


    @app.get("/settings/groups", response_class=HTMLResponse)
    async def settings_groups_page(request: Request):
        # 合并到 /settings/members
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/settings/members")

    @app.get("/settings/system", response_class=HTMLResponse)
    async def settings_system_page(request: Request):
        import os
        import subprocess
        import json as _j
        docker_status = {}
        for name in ["zoom-monitor", "zoom-api", "zoom-webhook", "zoom-command"]:
            try:
                r = subprocess.run(["curl", "-s", "--unix-socket", "/var/run/docker.sock", f"http://localhost/containers/{name}/json"], capture_output=True, text=True, timeout=5)
                if r.returncode == 0 and r.stdout:
                    data = _j.loads(r.stdout)
                    docker_status[name] = data.get("State", {}).get("Status", "unknown")
                else:
                    docker_status[name] = "unknown"
            except:
                docker_status[name] = "unknown"

        zoom_status = {"webhook_delay_text": "-", "meeting_count": 0, "online_count": 0, "sharing_count": 0}
        try:
            conn = db._get_conn()
            last_wh = conn.execute("SELECT MAX(created_at) FROM zoom_events").fetchone()[0]
            if last_wh:
                from datetime import datetime as _dt, timezone as _tz
                try:
                    lc = last_wh.replace("Z", "+00:00")
                    ld = _dt.fromisoformat(lc)
                    delta = (_dt.now(_tz.utc) - ld).total_seconds()
                    if delta < 60:
                        zoom_status["webhook_delay_text"] = "刚刚"
                    else:
                        zoom_status["webhook_delay_text"] = f"{int(delta/60)}分钟前"
                except:
                    pass
        except:
            pass

        # Zoom 凭证数据(原 settings_zoom_page)
        conn = db._get_conn()
        tokens = []
        try:
            tokens = conn.execute("SELECT email, scope, expires_at FROM zoom_oauth_tokens ORDER BY id DESC LIMIT 3").fetchall()
        except Exception:
            tokens = []
        webhook_status = "未配置"
        try:
            import requests as req
            h = req.get("http://127.0.0.1:9000/health", timeout=3)
            webhook_status = "运行中" if h.status_code == 200 else "异常"
        except: webhook_status = "未启动"

        # Telegram Bot 数据(原 settings_telegram_page)
        bot_ok = False
        bot_username = ""
        try:
            import requests as req
            token = settings.telegram_bot_token
            r = req.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
            if r.json().get("ok"):
                bot_ok = True
                bot_username = r.json()["result"]["username"]
        except: pass

        return tmpl.TemplateResponse(request, "settings_system.html", {
            "brand": BRAND,
            "version": "0.2.1",
            "docker_status": docker_status,
            "zoom_status": zoom_status,
            "participant_count": db.get_today_participants(limit=1) and len(db.get_today_participants(limit=10000)) or 0,
            # Zoom 凭证
            "monitor_interval": getattr(settings, "monitor_interval", 300),
            "pmi_id": getattr(settings, "zoom_pmi_id", ""),
            "extra_ids": getattr(settings, "zoom_extra_meeting_ids", ""),
            "host_email": getattr(settings, "zoom_host_email", ""),
            "webhook_status": webhook_status,
            "oauth_accounts": [{"email": r[0], "scope": (r[1] or "")[:50], "expires_at": r[2]} for r in tokens],
            # Telegram Bot
            "bot_ok": bot_ok,
            "bot_username": bot_username,
            "home_chat_id": "7922047310",
        })
    # ── API ──────────────────────────────────────────────────────────────────
    @app.post("/api/tg/send-test")
    async def api_tg_send_test():
        """发送测试消息到 Telegram"""
        token = "8791140288:AAHL_7Az6vQitTIJUhlP-M8YaMXzPz2joG4"
        now_str = datetime.now(MYT).strftime("%m-%d %H:%M:%S")
        try:
            from services.telegram import TelegramService
            tg = TelegramService(token=token, chat_id="7922047310")
            result = tg.send(f"🧪 Zoom Monitor 测试消息\n\nTelegram 推送正常 ✅\n发送时间: {now_str} MYT")
            return {"ok": result.get("ok", False), "sent_at": now_str}
        except Exception as e:
            return {"ok": False, "error": str(e), "sent_at": now_str}

    @app.get("/api/health-check")
    async def api_health_check():
        """系统健康检查"""
        import subprocess, requests as req
        result = {}
        # Zoom API
        try:
            z = req.post("https://zoom.us/oauth/token", data={"grant_type":"account_credentials","account_id":"Vw3Dd_xwTg67X3dvpGm3RA"}, auth=("5yeuUYQKSnKB9fKDHrmuw","tis2wWnK3XKMK1qFcAGRV42fNZV7iFjS"), timeout=5)
            result["zoom_api"] = "ok" if z.status_code == 200 else "error"
        except: result["zoom_api"] = "timeout"
        # Webhook
        try:
            w = req.get("http://zoom-webhook:9000/health", timeout=3)
            result["webhook"] = "ok" if w.status_code == 200 else "error"
            if result["webhook"] == "ok":
                try:
                    conn = db._get_conn()
                    last = conn.execute("SELECT MAX(created_at) FROM zoom_events").fetchone()[0]
                    if last:
                        from datetime import datetime, timezone, timedelta
                        MYT = timezone(timedelta(hours=8))
                        try:
                            last_clean = last.replace("Z", "+00:00")
                            if "+" not in last_clean and last_clean.count("-") >= 2:
                                last_clean += "+00:00"
                            last_dt = datetime.fromisoformat(last_clean)
                            result["webhook_last_event"] = last_dt.astimezone(MYT).strftime("%m-%d %H:%M:%S")
                            delta = (datetime.now(timezone.utc) - last_dt).total_seconds()
                            result["webhook_last_event_age_seconds"] = int(delta)
                            if delta < 60:
                                result["webhook_last_event_age_text"] = "刚刚"
                            elif delta < 900:
                                result["webhook_last_event_age_text"] = f"{int(delta//60)} 分钟前"
                            elif delta < 3600:
                                result["webhook_last_event_age_text"] = f"{int(delta//60)} 分钟前"
                            else:
                                result["webhook_last_event_age_text"] = f"{int(delta//3600)} 小时前"
                            if delta <= 900:
                                result["webhook_status"] = "healthy"
                            elif delta <= 3600:
                                result["webhook_status"] = "warning"
                            else:
                                result["webhook_status"] = "offline"
                        except Exception as e:
                            result["webhook_status"] = f"unknown({str(e)[:30]})"
                    else:
                        result["webhook_status"] = "offline"
                        result["webhook_last_event_age_text"] = "无事件"
                except:
                    result["webhook_status"] = "unknown"
        except: result["webhook"] = "timeout"
        # Telegram
        try:
            token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
            tg = req.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5) if token else type("X", (object,), {"status_code": 400})()
            result["telegram"] = "ok" if tg.status_code == 200 and tg.json().get("ok") else "error"
        except: result["telegram"] = "timeout"
        # sub2api
        try:
            s2 = req.post("https://sub2api.dhbwang.xyz/v1/chat/completions", json={"model":"gpt-5.4-mini","messages":[{"role":"user","content":"hi"}],"max_tokens":5}, headers={"Authorization":"Bearer " + (os.environ.get("SUB2API_KEY") or os.environ.get("DEEPSEEK_API_KEY", ""))}, timeout=8)
            result["sub2api"] = "ok" if s2.status_code == 200 else "error"
        except: result["sub2api"] = "timeout"
        # DB
        try:
            conn = db._get_conn()
            cnt = conn.execute("SELECT COUNT(*) FROM zoom_participants").fetchone()[0]
            conn.close()
            result["database"] = f"ok ({cnt} records)"
        except Exception as e:
            result["database"] = f"error: {e}"
        # Docker
        docker_status = {}
        for name in ["zoom-monitor","zoom-api","zoom-webhook","zoom-command"]:
            try:
                r = subprocess.run(["docker","inspect",name,"--format","{{.State.Status}}"], capture_output=True, text=True, timeout=3)
                docker_status[name] = r.stdout.strip()
            except: docker_status[name] = "unknown"
        result["docker"] = docker_status
        # 跨租户分组自检
        try:
            conn = db._get_conn()
            bad = conn.execute("""
                SELECT COUNT(*) FROM member_display md
                LEFT JOIN member_groups mg ON md.group_id = mg.id
                WHERE md.group_id IS NOT NULL
                  AND mg.tenant_id != md.tenant_id
            """).fetchone()[0]
            # 检测 member_group_members 表是否有跨租户脏数据
            bad_mgm = conn.execute("""
                SELECT COUNT(*) FROM member_group_members mgm
                JOIN member_display md ON md.raw_name = mgm.member_name
                JOIN member_groups mg ON mg.id = mgm.group_id
                WHERE md.tenant_id != mg.tenant_id
            """).fetchone()[0]
            conn.close()
            result["group_integrity"] = {
                "ok": bad == 0,
                "cross_tenant_count": bad,
            }
            result["mgm_integrity"] = {
                "ok": bad_mgm == 0,
                "cross_tenant_count": bad_mgm,
            }
        except Exception as e:
            result["group_integrity"] = {"ok": False, "error": str(e)}
        return {"ok": True, **result}

    @app.get("/api/maintenance/cleanup-sessions")
    async def api_cleanup_sessions(request: Request, dry_run: bool = True):
        """扫描并修复 stale open sessions。dry_run=true（默认）只报告不修改。"""
        role = request.session.get("role", "")
        if role not in ("super_admin", "admin", "tenant_admin", "owner"):
            return JSONResponse(status_code=403, content={"ok": False, "error": "无权限"})
        tenant_id = request.app.state.get_effective_tenant_id(request) if hasattr(request.app.state, 'get_effective_tenant_id') else (request.session.get("selected_tenant") or request.session.get("tenant_id") or "default")
        result = db.cleanup_stale_sessions(tenant_id=tenant_id, dry_run=dry_run)
        return {"ok": True, **result}

    @app.get("/api/report-data")
    async def api_report_data(days: int = 7):
        """报表数据:趋势 + 排行"""
        import sqlite3 as _sql
        _conn = _sql.connect("/app/data/tracking.db")
        now = datetime.now(timezone.utc)
        trend = []
        for i in range(days - 1, -1, -1):
            d = (now - timedelta(days=i)).strftime("%Y-%m-%d")
            cnt = _conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (d, (datetime.fromisoformat(d) + timedelta(days=1)).isoformat())).fetchone()[0]
            uniq = _conn.execute("SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (d, (datetime.fromisoformat(d) + timedelta(days=1)).isoformat())).fetchone()[0]
            trend.append({"date": d, "count": cnt, "unique": uniq})
        today = now.strftime("%Y-%m-%d")
        top = _conn.execute("SELECT name, COUNT(*) as cnt FROM zoom_participants WHERE action_time >= ? GROUP BY name ORDER BY cnt DESC LIMIT 20", (today,)).fetchall()
        _conn.close()
        return {"ok": True, "trend": trend, "top": [{"name": r[0], "count": r[1]} for r in top]}

    @app.get("/api/participants")
    async def api_participants(request: Request, limit: int = 200):
        if settings.demo_mode:
            return _ensure_demo().get_demo_participants(limit=limit)
        return db.get_today_participants(limit=limit, tenant_id=request.app.state.get_effective_tenant_id(request))

    @app.get("/api/v3/attendance-summary")
    async def api_v3_attendance_summary(request: Request):
        if settings.demo_mode:
            return _ensure_demo().get_demo_attendance_summary()
        try:
            tenant_id = request.app.state.get_effective_tenant_id(request)
            from services.zoom import ZoomService
            zoom = ZoomService()
            result = await zoom.get_today_attendance_summary(tenant_id)
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/alerts")
    async def api_alerts(request: Request, limit: int = 50):
        if settings.demo_mode:
            return _ensure_demo().get_demo_alerts(limit=limit)
        return db.get_recent_alerts(limit=limit, tenant_id=request.app.state.get_effective_tenant_id(request))

    @app.get("/api/events")
    async def api_events(request: Request, limit: int = 50):
        if settings.demo_mode:
            return _ensure_demo().get_demo_events(limit=limit)
        return db.get_recent_events(limit=limit, tenant_id=request.app.state.get_effective_tenant_id(request))

    @app.get("/api/reports")
    async def api_reports():
        if settings.demo_mode:
            return _ensure_demo().get_demo_reports()
        return []

    @app.get("/api/analytics")
    async def api_analytics():
        if settings.demo_mode:
            return _ensure_demo().get_demo_analytics()
        return {}

    @app.get("/api/stats")
    async def api_stats(request: Request):
        if settings.demo_mode:
            return _ensure_demo().get_demo_stats()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        participants = db.get_today_participants(limit=200, tenant_id=tenant_id)
        alerts = db.get_recent_alerts(limit=50, tenant_id=tenant_id)
        return {
            "participant_count": len(participants),
            "alert_count": len(alerts),
        }

    # ── Webhook ──────────────────────────────────────────────────────────────
    @app.get("/webhook")
    async def zoom_webhook_get(request: Request):
        """Zoom 有时会用 GET 发 Challenge"""
        return {"ok": True, "message": "webhook active"}

    @app.post("/webhook")
    @app.post("/webhook/{tenant_id:str}")
    async def zoom_webhook(request: Request, tenant_id: str = None):
        if settings.demo_mode:
            return {"ok": True, "demo": True}

        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON")

        import hashlib as _hashlib
        import hmac as _hmac
        event_type = payload.get("event", "")
        sys.stdout.write(f"[WEBHOOK] Received event: {event_type}\n")
        sys.stdout.write(f"[WEBHOOK] Body preview: {body[:300].decode()}\n")
        sys.stdout.flush()

        # ── URL Challenge(验证端点)支持 per-account webhook_secret ──
        if event_type == "endpoint.url_validation":
            # 废弃 tenant(如 wangtest)的 Challenge 必须被明确拒绝
            if tenant_id:
                _tenant_accts = db.get_zoom_accounts(tenant_id)
                _tenant_active = next((a for a in _tenant_accts if a.get("is_active")), None)
                if not _tenant_active or not _tenant_active.get("webhook_secret"):
                    sys.stderr.write(f"[WEBHOOK] unknown tenant webhook path: {tenant_id} "
                                     f"(rejected at url_validation)\n")
                    sys.stderr.flush()
                    raise HTTPException(403, "unknown tenant")
            plain_token = payload.get("payload", {}).get("plainToken", "")
            _secret = settings.zoom_webhook_secret
            if tenant_id:
                _accts = db.get_zoom_accounts(tenant_id)
                _active = next((a for a in _accts if a.get("is_active")), None)
                if _active and _active.get("webhook_secret"):
                    _secret = _active["webhook_secret"]
                    sys.stdout.write(f"[WEBHOOK:{tenant_id}] Using per-account webhook_secret\n")
            else:
                # 无 tenant_id:从所有活跃账号中取第一个有 webhook_secret 的
                try:
                    # db.py 中没有 get_all_active_zoom_accounts 的公开导出
                    # 直接从 tenant_users 反查所有活跃租户
                    _conn = db._get_conn()
                    _rows = _conn.execute(
                        "SELECT webhook_secret FROM zoom_accounts WHERE is_active=1 AND webhook_secret != '' LIMIT 1"
                    ).fetchall()
                    if _rows and _rows[0]["webhook_secret"]:
                        _secret = _rows[0]["webhook_secret"]
                        sys.stdout.write(f"[WEBHOOK] Using db webhook_secret (non-default)\n")
                except Exception:
                    pass
            enc = _hmac.new(_secret.encode(), plain_token.encode(), _hashlib.sha256).hexdigest()
            sys.stdout.write(f"[WEBHOOK] Challenge OK: pt={plain_token[:10]}... enc={enc[:10]}...\n")
            sys.stdout.flush()
            return {"plainToken": plain_token, "encryptedToken": enc}

        # ── 从 payload 中提取 account_id → 反查 per-account secret ──
        # 放在签名验证之前，因为每个 Zoom App 有自己的 webhook_secret
        _sig_account_id = payload.get("payload", {}).get("account_id", "") or payload.get("account_id", "")
        # 当 tenant_id 路径参数存在时，不允许 fallback 到全局 secret
        # 废弃 tenant(如 wangtest)的 webhook 请求必须被明确拒绝
        if tenant_id:
            _tenant_accts = db.get_zoom_accounts(tenant_id)
            _tenant_active = next((a for a in _tenant_accts if a.get("is_active")), None)
            if not _tenant_active or not _tenant_active.get("webhook_secret"):
                sys.stderr.write(f"[WEBHOOK] unknown tenant webhook path: {tenant_id} "
                                 f"(no active zoom_account with webhook_secret)\n"
                                 f"account_id={_sig_account_id[:30] if _sig_account_id else 'none'}\n")
                sys.stderr.flush()
                raise HTTPException(403, "unknown tenant")
            _sig_secret = _tenant_active["webhook_secret"]
        else:
            _sig_secret = settings.zoom_webhook_secret
            if _sig_account_id:
                _sig_tenant = db.get_tenant_id_by_zoom_account(_sig_account_id)
                if _sig_tenant:
                    _accts = db.get_zoom_accounts(_sig_tenant)
                    _active = next((a for a in _accts if a.get("is_active") and a.get("account_id") == _sig_account_id), None)
                    if _active and _active.get("webhook_secret"):
                        _sig_secret = _active["webhook_secret"]

        signature = request.headers.get("x-zm-signature", "")
        ts = request.headers.get("x-zm-request-timestamp", "")
        sys.stdout.write(f"[WEBHOOK] Headers: sig={signature[:50]}... ts={ts}\n")
        sys.stdout.write(f"[WEBHOOK] Body for sig: {body[:200]}\n")
        if _sig_secret and signature:
            ts = request.headers.get("x-zm-request-timestamp", "")
            sys.stdout.write(f"[WEBHOOK] sig check: ts={ts} body_len={len(body)}\n")
            sys.stdout.flush()
            # Zoom 签名: v0=HMAC_SHA256(secret, "v0:" + timestamp + ":" + body)
            msg = f"v0:{ts}:".encode() + body
            expected = hmac.new(_sig_secret.encode(), msg, _hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, f"v0={expected}"):
                sys.stderr.write(f"[WEBHOOK] 签名验证失败: v0={expected[:30]}... got={signature[:40]}...\n")
                body_text = body.decode() if isinstance(body, bytes) else str(body)
                sys.stderr.write(f"[WEBHOOK] 拒绝伪造请求: body={body_text[:200]}\n")
                sys.stderr.flush()
                raise HTTPException(403, "signature mismatch")

        event_type = payload.get("event", "")
        # 提取 Zoom account_id → 反查 tenant_id → 数据隔离
        account_id = payload.get("payload", {}).get("account_id", "") or payload.get("account_id", "")
        webhook_tenant_id = db.get_tenant_id_by_zoom_account(account_id) if account_id else None
        db.save_webhook_event(event_type, payload, tenant_id=webhook_tenant_id or "unknown")
        sys.stdout.write(f"[WEBHOOK] {event_type}")
        if webhook_tenant_id:
            sys.stdout.write(f" tenant={webhook_tenant_id}")
        sys.stdout.write(f" account={account_id[:20] if account_id else 'none'}\n")
        sys.stdout.flush()

        # 参与者事件 -> ParticipantService
        if "participant" in event_type and any(
            kw in event_type for kw in ["joined", "left", "admitted", "waiting_room"]
        ):
            from services.participant import ParticipantService

            obj = payload.get("payload", {}).get("object", payload.get("object", {}))
            participant = obj.get("participant", {})
            meeting_id = str(obj.get("id", ""))
            if "breakout" in event_type:
                meeting_id = str(payload.get("payload", {}).get("object", {}).get("id", ""))
            ParticipantService.save_webhook_participant(
                event_type, participant, meeting_id,
                tenant_id=webhook_tenant_id or "unknown",
            )

        # Sharing events -> ParticipantService
        if "sharing" in event_type:
            from services.participant import ParticipantService

            obj = payload.get("payload", {}).get("object", payload.get("object", {}))
            participant = obj.get("participant", {})
            meeting_id = str(obj.get("id", ""))
            ParticipantService.save_webhook_sharing(
                event_type, participant, meeting_id,
                tenant_id=webhook_tenant_id or "unknown",
            )

        # ── Webhook Telegram Push (走 AlertService) ──────────────────────
        try:
            webhook_tenant_id = webhook_tenant_id or "default"
            from services.alert import AlertService
            alert_service = AlertService()
            alert_service.handle_webhook_event(
                payload=payload,
                event_type=event_type,
                tenant_id=webhook_tenant_id,
                account_id=account_id,
                bot_token_override="",
            )
        except Exception as e:
            sys.stderr.write(f"[WEBHOOK_PUSH] error: {e}\n")
            sys.stderr.flush()
        # ────────────────────────────────────────────────────────────────
        
        return {"ok": True}

    # ── 健康检查 ─────────────────────────────────────────────────────────────

    @app.get("/settings/members", response_class=HTMLResponse)
    async def settings_members_page(request: Request):
        user_id = request.session.get("user_id")
        user = db.get_user_by_id(user_id) if user_id else None
        return tmpl.TemplateResponse(request, "settings_members.html", {
            "brand": BRAND,
            "current_user": user,
        })

    @app.get("/api/v3/telegram-channels")
    async def api_v3_get_telegram_channels():
        channels = db.get_telegram_channels()
        return {"ok": True, "channels": channels}

    @app.post("/api/v3/telegram-channels")
    async def api_v3_create_telegram_channel(request: Request):
        data = await request.json()
        cid = db.upsert_telegram_channel(data)
        db.log_audit("create", "telegram_channel", cid, f"Created channel: {data.get('name','')} (chat_id={data.get('chat_id','')})")
        return {"ok": True, "id": cid}

    @app.put("/api/v3/telegram-channels/{chat_id}")
    async def api_v3_update_telegram_channel(chat_id: str, request: Request):
        data = await request.json()
        data["chat_id"] = chat_id
        cid = db.upsert_telegram_channel(data)
        db.log_audit("update", "telegram_channel", cid, f"Updated channel: {data.get('name','')}")
        return {"ok": True, "id": cid}

    @app.delete("/api/v3/telegram-channels/{chat_id}")
    async def api_v3_delete_telegram_channel(chat_id: str):
        name = db.get_telegram_channel(chat_id)
        n = name.get("name", "") if name else ""
        db.delete_telegram_channel(chat_id)
        db.log_audit("delete", "telegram_channel", chat_id, f"Deleted channel: {n} (chat_id={chat_id})")
        return {"ok": True}

    @app.post("/api/v3/telegram-channels/{chat_id}/test")
    async def api_v3_test_telegram_channel(chat_id: str):
        channel = db.get_telegram_channel(chat_id)
        if not channel:
            return {"ok": False, "error": "channel not found"}
        name = channel.get("name", "")
        from telegram_push import send_message
        result = send_message(chat_id=chat_id, text="\u2705 \u8fd9\u662f\u4e00\u6761\u6d4b\u8bd5\u6d88\u606f\n\n\u9891\u9053\uff1a" + name + "\nID\uff1a" + chat_id + "\n\n\u5982\u679c\u6536\u5230\u6b64\u6d88\u606f\uff0c\u8bf4\u660e Telegram \u901a\u77e5\u914d\u7f6e\u6b63\u786e\u3002")
        ok = result.get("ok", False)
        db.log_audit("test", "telegram_channel", chat_id, f"Test message sent to channel {name} ({chat_id}): success={ok}")
        return {"ok": True, "message": "\u6d4b\u8bd5\u6d88\u606f\u53d1\u9001\u6210\u529f"}

    @app.get("/settings/telegram-channels", response_class=HTMLResponse)
    async def settings_telegram_channels_page(request: Request):
        # 合并到 /dashboard/alerts
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/dashboard/alerts")

    @app.get("/api/v3/aliases")
    async def api_v3_aliases(request: Request):
        """获取所有别名配置（按租户隔离）"""
        tenant_id = request.app.state.get_effective_tenant_id(request) if hasattr(request, "app") else None
        # 兼容旧 API 输出结构 {ok, aliases: [{id, canonical_name, alias_name, count_enabled, note, created_at, updated_at}]}
        from services.member import MemberService
        ms = MemberService()
        aliases = ms.get_aliases(tenant_id)
        # 将 JSON 数组中的第一个 alias 作为 alias_name 展示
        result = []
        for a in aliases:
            alias_list = json.loads(a.get("note", "[]") or "[]")
            if alias_list:
                for a_name in alias_list:
                    result.append({
                        "id": a["id"],
                        "canonical_name": a["canonical_name"],
                        "alias_name": a_name,
                        "count_enabled": a["count_enabled"],
                        "note": "",
                        "created_at": a["created_at"],
                        "updated_at": a["updated_at"],
                    })
            else:
                result.append(dict(a))
        return {"ok": True, "aliases": result}

    @app.post("/api/v3/aliases")
    async def api_v3_add_alias(request: Request):
        """添加别名映射（按租户隔离）"""
        data = await request.json()
        canonical = data.get("canonical_name", "").strip()
        alias = data.get("alias_name", "").strip()
        if not canonical or not alias:
            return {"ok": False, "error": "参数不完整"}
        count_enabled = data.get("count_enabled", 1)
        note = data.get("note", "")
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.member import MemberService
        ms = MemberService()
        return ms.add_alias(canonical, alias, tenant_id, note, bool(count_enabled))



    @app.get("/api/v3/member-aliases/discover")
    async def api_v3_member_aliases_discover_alias(request: Request):
        """别名:/api/v3/member-aliases/discover -> 同 /api/v3/aliases/discover"""
        conn = db._get_conn()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        now_str = datetime.now(timezone.utc).isoformat()
        if tenant_id:
            rows = conn.execute("""SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND tenant_id=? GROUP BY name ORDER BY cnt DESC""", (cutoff, now_str, tenant_id)).fetchall()
        else:
            rows = conn.execute("""SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen FROM zoom_participants WHERE action_time >= ? AND action_time < ? GROUP BY name ORDER BY cnt DESC""", (cutoff, now_str)).fetchall()
        alias_rows = conn.execute("SELECT alias_name FROM member_aliases").fetchall()
        configured_aliases = set()
        for (alias_name,) in alias_rows:
            configured_aliases.add(alias_name.strip().lower().replace(" ", ""))
        display_rows = conn.execute("SELECT raw_name, aliases FROM member_display").fetchall()
        for r in display_rows:
            try:
                for a in json.loads(r["aliases"] or "[]"):
                    configured_aliases.add(a.strip().lower().replace(" ", ""))
            except: pass
        unmapped = []
        for r in rows:
            key = r["name"].strip().lower().replace(" ", "")
            if key not in configured_aliases:
                unmapped.append(r["name"])
        # 获取当前在线名单
        online_names = []
        try:
            zm, _ = _get_tenant_zoom_metrics(request)
            if zm is None:
                live_data = {"meetings": []}
            else:
                live_data = await zm.get_live()
            for m in live_data.get("meetings", []):
                for p in m.get("participants", []):
                    online_names.append(p.get("name", ""))
        except:
            pass
        return {"ok": True, "unmapped": list(set(unmapped)), "online": online_names}

    @app.get("/api/v3/member-names")
    async def api_v3_member_names(request: Request):
        """返回当前租户的历史用户名(去重)"""
        tenant_id = request.app.state.get_effective_tenant_id(request)
        conn = db._get_conn()
        if tenant_id:
            rows = conn.execute(
                "SELECT DISTINCT name FROM zoom_participants WHERE tenant_id=? ORDER BY name",
                (tenant_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT DISTINCT name FROM zoom_participants ORDER BY name").fetchall()
        names = [r[0] for r in rows]
        return {"ok": True, "names": names}

    @app.get("/api/v3/aliases/discover")
    async def api_v3_discover(request: Request):
        """自动发现历史 Zoom 用户名，统计出现次数和是否在线"""
        conn = db._get_conn()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from datetime import datetime, timezone, timedelta
        
        # 统计最近30天的 Zoom 用户名出现次数
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        now_str = datetime.now(timezone.utc).isoformat()
        rows = conn.execute("""\
            SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen
            FROM zoom_participants
            WHERE action_time >= ? AND action_time < ?
            GROUP BY name
            ORDER BY cnt DESC
        """, (cutoff, now_str)).fetchall()
        # 但上面没加 tenant_id 隔离，现在加上
        if tenant_id:
            rows = conn.execute("""\
            SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen
            FROM zoom_participants
            WHERE action_time >= ? AND action_time < ? AND tenant_id=?
            GROUP BY name
            ORDER BY cnt DESC
        """, (cutoff, now_str, tenant_id)).fetchall()
        else:
            rows = conn.execute("""\
            SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen
            FROM zoom_participants
            WHERE action_time >= ? AND action_time < ?
            GROUP BY name
            ORDER BY cnt DESC
        """, (cutoff, now_str)).fetchall()
        
        # 加载已配置的别名(来自 member_aliases 表)
        alias_rows = conn.execute("SELECT alias_name FROM member_aliases").fetchall()
        configured_aliases = set()
        for (alias_name,) in alias_rows:
            configured_aliases.add(alias_name.strip().lower().replace(" ", ""))
        
        # 补充:也排除 member_display 中 aliases JSON 字段里的别名
        md_rows = conn.execute("SELECT aliases FROM member_display").fetchall()
        for (aliases_json,) in md_rows:
            if not aliases_json:
                continue
            try:
                aliases_list = json.loads(aliases_json)
                if isinstance(aliases_list, list):
                    for a in aliases_list:
                        if a and isinstance(a, str):
                            configured_aliases.add(a.strip().lower().replace(" ", ""))
            except:
                pass
        
        # 当前在线(来自 v3)
        unmapped_set = set()
        zm, _ = _get_tenant_zoom_metrics(request)
        live_data = await zm.get_live() if zm else {"meetings": []}
        # 补充来源:所有 zoom_participants 中出现过的用户名(按 tenant 隔离)
        if tenant_id:
            _all_names = conn.execute("SELECT DISTINCT name FROM zoom_participants WHERE tenant_id=? ORDER BY name", (tenant_id,)).fetchall()
        else:
            _all_names = conn.execute("SELECT DISTINCT name FROM zoom_participants ORDER BY name").fetchall()
        for (an,) in _all_names:
            _ak = an.strip().lower().replace(" ", "")
            if _ak not in configured_aliases:
                unmapped_set.add(an)
        online_names = set()
        for m in live_data.get("meetings", []):
            for p in m.get("participants", []):
                online_names.add(p.get("name", ""))
        
        # 检查每个历史名字是否已被映射
        results = []
        for name, cnt, last_seen in rows:
            name = name.strip()
            if not name:
                continue
            key = name.lower().replace(" ", "")
            already_mapped = key in configured_aliases
            is_online = name in online_names
            results.append({
                "name": name,
                "count": cnt,
                "last_seen": last_seen or "",
                "is_online": is_online,
                "already_mapped": already_mapped,
            })
        
        return {"ok": True, "names": results}

    @app.post("/api/v3/aliases/map")
    async def api_v3_map_alias(request: Request):
        """一键映射:将 Zoom 用户名映射到标准成员名（按租户隔离）"""
        data = await request.json()
        zoom_name = data.get("zoom_name", "").strip()
        canonical_name = data.get("canonical_name", "").strip()
        count_enabled = data.get("count_enabled", 1)
        note = data.get("note", "")
        
        if not zoom_name or not canonical_name:
            return {"ok": False, "error": "参数不完整"}
        
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.member import MemberService
        ms = MemberService()
        return ms.add_alias(canonical_name, zoom_name, tenant_id, note, bool(count_enabled))

    @app.delete("/api/v3/aliases/{alias_id}")
    async def api_v3_del_alias(alias_id: int, request: Request):
        """删除别名（按租户隔离）"""
        tenant_id = request.app.state.get_effective_tenant_id(request) if hasattr(request, "app") else None
        from services.member import MemberService
        ms = MemberService()
        return ms.delete_alias(alias_id, tenant_id)


    @app.get("/api/v3/aliases/duplicates")
    async def api_v3_aliases_duplicates(request: Request):
        """发现疑似重复的 Zoom 用户名(去空格 / 大小写 / 前4词)"""
        import re
        from datetime import datetime, timezone, timedelta
        from collections import defaultdict
        conn = db._get_conn()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        if tenant_id:
            rows = conn.execute(
                "SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen FROM zoom_participants WHERE action_time >= ? AND tenant_id=? GROUP BY name ORDER BY cnt DESC",
                (cutoff, tenant_id)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen FROM zoom_participants WHERE action_time >= ? GROUP BY name ORDER BY cnt DESC",
                (cutoff,)
            ).fetchall()
        names = []
        for r in rows:
            nm = (r["name"] or "").strip()
            if nm:
                names.append({"name": nm, "count": r["cnt"], "last_seen": (r["last_seen"] or "")})
        def norm_no_space(s): return re.sub(r'\s+', '', s.lower())
        def norm_case(s): return s.lower().strip()
        def norm_first_4(s): return " ".join(s.strip().lower().split()[:4])
        seen = set()
        groups = []
        for fn in [norm_no_space, norm_case, norm_first_4]:
            buckets = defaultdict(list)
            for n in names:
                k = fn(n["name"])
                if k: buckets[k].append(n["name"])
            for k, members in buckets.items():
                if len(members) < 2: continue
                members.sort()
                gid = "|".join(members)
                if gid in seen: continue
                seen.add(gid)
                nl = {n["name"]: n for n in names}
                md = sorted([nl[m] for m in members], key=lambda x: -x["count"])
                groups.append({"group_id": hash(gid), "members": md, "suggested_primary": md[0]["name"]})
        groups.sort(key=lambda g: -len(g["members"]))
        return {"ok": True, "groups": groups, "total": len(groups)}

    @app.post("/api/v3/aliases/merge")
    async def api_v3_aliases_merge(request: Request):
        """批量合并:将一组别名合并到标准名（按租户隔离）"""
        data = await request.json()
        canonical = (data.get("canonical", "") or "").strip()
        aliases = data.get("aliases", [])
        if not canonical or not aliases:
            return {"ok": False, "error": "参数不完整"}
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.member import MemberService
        ms = MemberService()
        ok = True
        cnt = 0
        for alias in aliases:
            if alias == canonical:
                continue
            r = ms.add_alias(canonical, alias, tenant_id)
            if r.get("ok"):
                cnt += 1
            else:
                ok = False
        return {"ok": ok, "mapped": cnt}


    @app.get("/api/v3/sharing-live")
    async def api_v3_sharing_live(request: Request):
        """共享状态：通过 ZoomService 统一来源，保持前向兼容"""
        tenant_id = request.app.state.get_effective_tenant_id(request)
        conn = db._get_conn()
        from services.zoom import ZoomService

        zoom = ZoomService()
        sharers = await zoom.get_current_sharing(tenant_id)

        # ── 构建前向兼容的输出结构 ──
        now_utc = datetime.now(timezone.utc)

        def _lookup_group(raw_name):
            _g = conn.execute(
                "SELECT md.group_id, mg.name FROM member_display md "
                "LEFT JOIN member_groups mg ON mg.id=md.group_id AND mg.tenant_id=md.tenant_id "
                "WHERE (md.raw_name=? OR md.display_name=?) AND md.tenant_id=?",
                (raw_name, raw_name, tenant_id)
            ).fetchone()
            if _g and _g[0]:
                return {"group_id": str(_g[0]), "group_name": _g[1] or ""}
            return {"group_id": "", "group_name": ""}

        def to_myt(dt_str):
            if not dt_str: return ""
            try:
                d = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                return d.astimezone(MYT).strftime("%m-%d %H:%M:%S")
            except: return dt_str[:16]

        def disp_mins(m):
            return f"{m//60}h{m%60:02d}" if m >= 60 else f"{m}分钟"

        active = []
        for s in sharers:
            g = _lookup_group(s.get("raw_name", ""))
            st = s.get("start_time", "")
            mins = s.get("duration_minutes", 0)
            active.append({
                "name": s.get("display_name", s.get("name", "")),
                "raw_name": s.get("raw_name", ""),
                "user_id": s.get("user_id", ""),
                "meeting_id": s.get("meeting_id", ""),
                "content": s.get("content", ""),
                "start_time": st,
                "start_time_display": to_myt(st),
                "duration_minutes": mins,
                "duration_display": disp_mins(mins),
                "source": "zoom_service",
                "group_id": g.get("group_id", ""),
                "group_name": g.get("group_name", ""),
            })

        # 分组统计
        groups_stats = {}
        for entry in active:
            gid = entry.get("group_id", "")
            gname = entry.get("group_name", "未分组")
            if gid:
                key = f"{gid}_{gname}"
                if key not in groups_stats:
                    groups_stats[key] = {"group_id": gid, "group_name": gname, "count": 0}
                groups_stats[key]["count"] += 1

        all_groups = conn.execute(
            "SELECT id, name FROM member_groups WHERE tenant_id=? ORDER BY id",
            (tenant_id,)
        ).fetchall()
        groups_list = []
        for gid, gname in all_groups:
            sgid = str(gid)
            key = f"{sgid}_{gname}"
            cnt = groups_stats[key]["count"] if key in groups_stats else 0
            groups_list.append({"group_id": sgid, "group_name": gname, "count": cnt})
        ungrouped_cnt = sum(1 for e in active if not e.get("group_id"))
        groups_list.insert(0, {"group_id": "", "group_name": "全部", "count": len(active)})

        _tenant = db.get_tenant(tenant_id) if tenant_id else None
        cap = {
            "zoom_plan": _tenant.get("zoom_plan", "unknown"),
            "live_mode": _tenant.get("live_mode", "metrics"),
            "sharing_mode": _tenant.get("sharing_mode", "metrics"),
            "metrics_available": _tenant.get("metrics_available", 0),
        } if _tenant else None

        return {
            "ok": True,
            "current": len(active),
            "active": active,
            "recent": [],
            "recent_total": 0,
            "groups": groups_list,
            "ungrouped": ungrouped_cnt,
            "sources": {"zoom_service": len(active)},
            "capability": cap,
        }




    @app.get("/api/v3/sharing-debug")
    async def api_v3_sharing_debug(request: Request):
        """调试:最近 sharing 事件 + 当前 sharing_live 表(按当前 session 租户过滤)"""
        conn = db._get_conn()
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        cutoff = (now_utc - timedelta(minutes=30)).isoformat()
        _tenant = request.app.state.get_effective_tenant_id(request)
        events = conn.execute(
            "SELECT id, event_type, created_at, payload FROM zoom_events WHERE event_type LIKE '%sharing%' AND created_at >= ? AND tenant_id=? ORDER BY created_at DESC LIMIT 20",
            (cutoff, _tenant)
        ).fetchall()
        import json as _json
        event_list = []
        for e in events:
            try:
                p = _json.loads(e[3])
                obj = p.get("payload", {}).get("object", {})
                pt = obj.get("participant", {})
                sd = pt.get("sharing_details", {})
                event_list.append({
                    "id": e[0], "event_type": e[1], "created_at": e[2],
                    "user_name": pt.get("user_name", ""),
                    "user_id": str(pt.get("user_id", "")),
                    "content": sd.get("content", ""),
                    "date_time": sd.get("date_time", ""),
                })
            except: pass
        
        live_rows = conn.execute(
            "SELECT * FROM sharing_live WHERE is_active=1 AND tenant_id=? "
            "AND id IN ("
            "  SELECT MAX(id) FROM sharing_live "
            "  WHERE is_active=1 AND tenant_id=? "
            "  GROUP BY tenant_id, meeting_id, user_name"
            ")",
            (_tenant, _tenant),
        ).fetchall()
        live_cols = [c[1] for c in conn.execute("PRAGMA table_info(sharing_live)").fetchall()]
        active_sharing = [dict(zip(live_cols, r)) for r in live_rows]
        
        # Recovery candidates: started in last 2h without ended
        recovery = []
        for (payload_json,) in conn.execute(
            "SELECT payload FROM zoom_events WHERE event_type LIKE '%sharing%' AND created_at >= ? AND tenant_id=? ORDER BY created_at DESC",
            ((now_utc - timedelta(hours=2)).isoformat(), _tenant)
        ).fetchall():
            try:
                p = _json.loads(payload_json)
                et = p.get("event", "")
                obj = p.get("payload", {}).get("object", p.get("object", {}))
                pt = obj.get("participant", {})
                uid = re.sub(r"[^0-9]", "", str(pt.get("user_id", "")))[:20]
                raw = pt.get("user_name", "").strip()
                sd = pt.get("sharing_details", {})
                mid = str(obj.get("id", ""))
                recovery.append({
                    "event_type": et, "user_name": raw, "user_id": uid,
                    "meeting_id": mid, "content": sd.get("content", ""),
                    "date_time": sd.get("date_time", ""),
                })
            except: pass
        # Get all unique share fields from Metrics API
        share_fields_sample = []
        try:
            import httpx as _httpx
            async def _fetch():
                async with _httpx.AsyncClient(timeout=5) as c:
                    tr = await c.post("https://zoom.us/oauth/token",
                        data={"grant_type": "account_credentials", "account_id": settings.zoom_account_id},
                        auth=(settings.zoom_client_id, settings.zoom_client_secret))
                    if tr.status_code == 200:
                        tok = tr.json().get("access_token", "")
                        import asyncio
                        mr = await c.get("https://api.zoom.us/v2/metrics/meetings?type=live&page_size=100",
                            headers={"Authorization": f"Bearer {tok}"})
                        if mr.status_code == 200:
                            for m in mr.json().get("meetings", []):
                                mid = str(m.get("id", ""))
                                pr = await c.get(f"https://api.zoom.us/v2/metrics/meetings/{mid}/participants?page_size=300",
                                    headers={"Authorization": f"Bearer {tok}"})
                                if pr.status_code == 200:
                                    for p in pr.json().get("participants", [])[:10]:
                                        share_fields_sample.append({
                                            "name": p.get("user_name",""),
                                            "share_application": p.get("share_application"),
                                            "share_desktop": p.get("share_desktop"),
                                            "share_whiteboard": p.get("share_whiteboard"),
                                        })
            loop = asyncio.new_event_loop()
            loop.run_until_complete(_fetch())
            loop.close()
        except: pass
        
        return {
            "ok": True,
            "events_2h": recovery,
            "sharing_live_active": active_sharing,
            "metrics_share_fields_sample": share_fields_sample,
        }
    @app.get("/sharing", response_class=HTMLResponse)
    async def sharing_page(request: Request):
        return tmpl.TemplateResponse(request, "sharing.html", {"brand": BRAND})

    @app.get("/api/v3/member-discover")
    async def api_v3_member_discover(request: Request):
        """自动发现历史 Zoom 用户名"""
        conn = db._get_conn()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        if tenant_id:
            rows = conn.execute(
                "SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen FROM zoom_participants WHERE action_time >= ? AND tenant_id=? GROUP BY name ORDER BY cnt DESC",
                (cutoff, tenant_id)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen FROM zoom_participants WHERE action_time >= ? GROUP BY name ORDER BY cnt DESC",
                (cutoff,)
            ).fetchall()
        
        # Check which are already mapped
        def to_myt_display(s):
            if not s: return ""
            try:
                d = datetime.fromisoformat(s.replace("Z", "+00:00"))
                return d.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M")
            except: return s[:16]
        
        results = []
        for name, cnt, last_seen in rows:
            resolved = db.resolve_display_name(name, tenant_id=tenant_id)
            results.append({
                "raw_name": name,
                "display_name": resolved["display_name"],
                "count_enabled": resolved["count_enabled"],
                "is_mapped": resolved["display_name"] != name,
                "count": cnt,
                "last_seen": last_seen or "",
                "last_seen_display": to_myt_display(last_seen),
            })
        return {"ok": True, "names": results}

    @app.get("/api/v3/members")
    async def api_v3_members_alias(request: Request):
        """成员列表 — 统一 API，数据来源：get_today_attendance_summary() (Session-based)
        
        排序:在线优先 → 今日时长高到低
        返回字段与 get_today_attendance_summary 一致，不再使用 zoom_participants 旧算法。
        """
        conn = db._get_conn()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        role = request.session.get("role")
        if tenant_id:
            rows = conn.execute("SELECT * FROM member_display WHERE tenant_id=? AND deleted=0 ORDER BY display_name", (tenant_id,)).fetchall()
        elif role == "super_admin":
            rows = conn.execute("SELECT * FROM member_display WHERE deleted=0 ORDER BY display_name").fetchall()
        else:
            rows = []
        cols = [c[1] for c in conn.execute("PRAGMA table_info(member_display)").fetchall()]
        items = []
        for r in rows:
            item = dict(zip(cols, r))
            if isinstance(item.get("aliases"), str):
                try:
                    item["aliases"] = json.loads(item["aliases"])
                except:
                    item["aliases"] = []
            items.append(item)
        
        # ── 统一来源：get_today_attendance_summary() ──
        summary = db.get_today_attendance_summary(tenant_id=tenant_id)
        member_map = {}
        for m in summary.get("members", []):
            display = m.get("standard_name", "")
            for alias in [display, m.get("raw_name", "")] + (m.get("aliases") or []):
                if alias:
                    member_map[alias.lower().replace(" ", "")] = m
        
        now_utc = datetime.now(timezone.utc)
        
        def _myt_short(raw):
            if not raw:
                return ""
            try:
                d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
                if d.tzinfo is None:
                    d = d.replace(tzinfo=timezone.utc)
                d_myt = d + timedelta(hours=8)
                return d_myt.strftime("%m-%d %H:%M")
            except:
                return ""
        
        for m in items:
            nm = m.get("display_name", "")
            nm_key = nm.lower().replace(" ", "")
            sm = member_map.get(nm_key)
            if not sm:
                # Try aliases
                for alias in (m.get("aliases") or []) + [m.get("raw_name", "")]:
                    ak = str(alias).lower().replace(" ", "")
                    if ak in member_map:
                        sm = member_map[ak]
                        break
            
            if sm:
                m["is_online"] = sm.get("status") == "online"
                m["today_seconds"] = sm.get("today_total_seconds", 0)
                m["today_total_duration"] = sm.get("today_total_duration", "0m")
                m["first_join"] = sm.get("first_join", "")
                m["first_join_display"] = sm.get("first_join_display", "")
                m["last_activity"] = sm.get("last_activity", "")
                m["last_activity_display"] = sm.get("last_activity_display", "")
                m["last_leave_time_display"] = sm.get("last_leave_time_display", "")
                m["session_count"] = sm.get("session_count", 0)
                m["disconnect_count"] = sm.get("disconnect_count", 0)
                m["email"] = sm.get("email", "")
            else:
                m["is_online"] = False
                m["today_seconds"] = 0
                m["today_total_duration"] = "0m"
                m["first_join"] = ""
                m["first_join_display"] = ""
                m["last_activity"] = ""
                m["last_activity_display"] = ""
                m["last_leave_time_display"] = ""
                m["session_count"] = 0
                m["disconnect_count"] = 0
                m["email"] = ""
                # Fallback: email from member_display
                if hasattr(m, "get") and m.get("email"):
                    pass
                else:
                    try:
                        row = conn.execute(
                            "SELECT email FROM zoom_participants WHERE name=? AND email IS NOT NULL AND email!='' ORDER BY action_time DESC LIMIT 1",
                            (nm,),
                        ).fetchone()
                        if row:
                            m["email"] = row[0]
                    except:
                        pass
                # last_leave: try from zoom_participants as fallback
                try:
                    lr = conn.execute(
                        "SELECT action_time FROM zoom_participants WHERE tenant_id=? AND action IN ('leave','left','breakout_leave') AND name=? ORDER BY action_time DESC LIMIT 1",
                        (tenant_id, nm),
                    ).fetchone()
                    if lr:
                        m["last_leave_time_display"] = _myt_short(lr[0])
                except:
                    pass
        
        items.sort(key=lambda x: (
            0 if x.get("is_online") else 1,
            -x.get("today_seconds", 0),
            x.get("display_name", "") or "",
        ))
        
        return {
            "ok": True,
            "items": items,
            "today_seconds_source": "unified_session",
            "data_source": "",
            "is_realtime": False,
            "metrics_online": False,
        }
    @app.post("/api/v3/members")
    async def api_v3_members_add(request: Request):
        """POST /api/v3/members - 创建/更新成员(前端JS调用)"""
        data = await request.json()
        raw_name = data.get("raw_name", "").strip()
        display_name = data.get("display_name", "").strip()
        aliases = data.get("aliases", [])
        group_id = data.get("group_id", None)
        tenant_id = request.app.state.get_effective_tenant_id(request)
        if not raw_name or not display_name:
            return {"ok": False, "error": "raw_name 和 display_name 不能为空"}
        _validate_group_tenant(group_id, tenant_id)
        import re
        match_key = re.sub(r'\s+', '', raw_name.lower())
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = db._get_conn()
        try:
            # Check if existing record (同一租户内)
            if tenant_id:
                existing = conn.execute(
                    "SELECT id FROM member_display WHERE (display_name=? OR raw_name=?) AND tenant_id=?",
                    (display_name, raw_name, tenant_id)
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT id FROM member_display WHERE display_name=? OR raw_name=?",
                    (display_name, raw_name)
                ).fetchone()
            if existing:
                conn.execute(
                    """UPDATE member_display SET raw_name=?, match_key=?, aliases=?, group_id=?, updated_at=?
                       WHERE id=?""",
                    (raw_name, match_key, json.dumps(aliases), group_id, now, existing[0])
                )
            else:
                conn.execute(
                    """INSERT INTO member_display (raw_name, display_name, match_key, aliases, group_id, tenant_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                    (raw_name, display_name, match_key, json.dumps(aliases), group_id, tenant_id, now, now)
                )
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/v3/member-display")
    async def api_v3_member_display_list(request: Request):
        """所有显示名映射(按 tenant 隔离)"""
        conn = db._get_conn()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        role = request.session.get("role")
        if tenant_id:
            rows = conn.execute("SELECT * FROM member_display WHERE tenant_id=? AND deleted=0 ORDER BY display_name", (tenant_id,)).fetchall()
        elif role == "super_admin":
            rows = conn.execute("SELECT * FROM member_display WHERE deleted=0 ORDER BY display_name").fetchall()
        else:
            rows = []
        cols = [c[1] for c in conn.execute("PRAGMA table_info(member_display)").fetchall()]
        items = []
        for r in rows:
            item = dict(zip(cols, r))
            if isinstance(item.get("aliases"), str):
                try:
                    item["aliases"] = json.loads(item["aliases"])
                except:
                    item["aliases"] = []
            items.append(item)
        return {"ok": True, "items": items, "today_seconds_source": "participant_sessions_empty"}

    @app.post("/api/v3/member-display")
    async def api_v3_member_display_add(request: Request):
        data = await request.json()
        raw_name = data.get("raw_name", "").strip()
        display_name = data.get("display_name", "").strip()
        count_enabled = data.get("count_enabled", 1)
        note = data.get("note", "")
        group_id = data.get("group_id", None)  # 分组 ID
        aliases = data.get("aliases", [])
        if not raw_name or not display_name:
            return {"ok": False, "error": "raw_name 和 display_name 不能为空"}
        tenant_id = request.app.state.get_effective_tenant_id(request)
        _validate_group_tenant(group_id, tenant_id)
        import re
        match_key = re.sub(r'\s+', '', raw_name.lower())
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = db._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO member_display (raw_name, display_name, match_key, count_enabled, note, group_id, aliases, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (raw_name, display_name, match_key, int(count_enabled), note, group_id, json.dumps(aliases), now, now)
            )
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.post("/api/v3/members/{display_name}/group")
    async def api_v3_set_member_group(display_name: str, request: Request):
        """设置成员的所属分组

        增强功能：
        - 按行 tenant_id 精确保存
        - sync_same_name_all_tenants（super_admin/admin 可选）：跨租户同步同名成员
        - 跨租户时按 group_name 找目标租户自己的 group_id，不会写错租户
        """
        data = await request.json()
        group_id = data.get("group_id", None)
        member_tenant_id = data.get("tenant_id", None) or None
        sync_all = data.get("sync_same_name_all_tenants", False)
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        session_tenant = request.app.state.get_effective_tenant_id(request)
        role = request.session.get("role")
        try:
            # 决定目标租户：优先行级 tenant_id
            target_tenant = member_tenant_id or session_tenant
            if not target_tenant:
                return {"ok": False, "error": "缺少 tenant_id"}

            # 1. 根据 group_id 查当前 group_name
            group_name = None
            if group_id is not None:
                row = conn.execute(
                    "SELECT name FROM member_groups WHERE id = ? AND tenant_id = ?",
                    (group_id, target_tenant)
                ).fetchone()
                if row is not None:
                    group_name = row["name"]
                # row is None: group 可能已被删除，允许置空

            # 2. 正常保存到当前租户
            _validate_group_tenant(group_id, target_tenant)
            match_key = display_name.strip().lower().replace(" ", "")
            cur = conn.execute(
                "UPDATE member_display SET group_id = ?, updated_at = ? WHERE tenant_id = ? AND deleted=0 AND (LOWER(display_name) = LOWER(?) OR LOWER(raw_name) = LOWER(?) OR LOWER(match_key) = LOWER(?))",
                (group_id, now, target_tenant, display_name, display_name, match_key)
            )
            if cur.rowcount == 0:
                conn.execute(
                    "INSERT INTO member_display (tenant_id, raw_name, display_name, match_key, group_id, deleted, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                    (target_tenant, display_name, display_name, match_key, group_id, now, now)
                )

            saved_tenants = [target_tenant]
            skipped_tenants = []

            # 3. sync_same_name_all_tenants：跨租户同步
            if sync_all and role in ("super_admin", "admin") and group_name is not None:
                # 找出同名成员在所有租户的分布
                same_rows = conn.execute(
                    "SELECT DISTINCT tenant_id FROM member_display "
                    "WHERE deleted=0 AND tenant_id != ? AND ("
                    "  LOWER(display_name) = LOWER(?)"
                    "  OR LOWER(raw_name) = LOWER(?)"
                    "  OR LOWER(match_key) = LOWER(?)"
                    ")",
                    (target_tenant, display_name, display_name, match_key)
                ).fetchall()

                for (t,) in same_rows:
                    # 在目标租户找同名 group_id
                    grp = conn.execute(
                        "SELECT id FROM member_groups WHERE name = ? AND tenant_id = ?",
                        (group_name, t)
                    ).fetchone()
                    if grp is None:
                        skipped_tenants.append(t)
                        continue
                    target_gid = grp["id"]
                    # 更新
                    sub = conn.execute(
                        "UPDATE member_display SET group_id = ?, updated_at = ? "
                        "WHERE tenant_id = ? AND deleted=0 AND ("
                        "  LOWER(display_name) = LOWER(?)"
                        "  OR LOWER(raw_name) = LOWER(?)"
                        "  OR LOWER(match_key) = LOWER(?)"
                        ")",
                        (target_gid, now, t, display_name, display_name, match_key)
                    )
                    if sub.rowcount == 0:
                        conn.execute(
                            "INSERT INTO member_display (tenant_id, raw_name, display_name, match_key, group_id, deleted, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
                            (t, display_name, display_name, match_key, target_gid, now, now)
                        )
                    saved_tenants.append(t)

            conn.commit()
            result = {"ok": True, "saved_tenants": saved_tenants}
            if skipped_tenants:
                result["skipped_tenants"] = skipped_tenants
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/v3/members/{display_name}/timeline")
    async def api_v3_member_timeline(display_name: str, request: Request):
        """返回某个成员的 enter→leave 分段 timeline（懒加载）"""
        from datetime import datetime, timezone, timedelta
        tenant_id = request.app.state.get_effective_tenant_id(request)
        current_meeting_id = request.query_params.get("meeting_id")
        session_start_after = request.query_params.get("session_start_after")
        source = request.query_params.get("source", "live").strip().lower()
        conn = db._get_conn()
        import re

        # ── 用 member_display 匹配规则查找该成员 ──
        resolved = db.resolve_display_name(display_name, tenant_id)
        resolved_display = resolved.get("display_name", display_name)
        raw_match = resolved.get("raw_name", "").strip()

        # ── 数据源：官方历史 ──
        if source == "history":
            md_rows = conn.execute(
                "SELECT raw_name, display_name, match_key, aliases FROM member_display WHERE tenant_id=? AND deleted=0",
                (tenant_id,)
            ).fetchall()
            matching_keys = set()
            def _add_key(key: str):
                k = re.sub(r'\s+', '', (key or "").strip().lower())
                if k:
                    matching_keys.add(k)
            target_key = re.sub(r'\s+', '', resolved_display.lower())
            _add_key(resolved_display)
            _add_key(raw_match)
            for row in md_rows:
                md_raw, md_disp, md_mk, md_alias_json = row
                mk_norm = re.sub(r'\s+', '', (md_mk or '').lower())
                raw_norm = re.sub(r'\s+', '', (md_raw or '').lower())
                disp_norm = re.sub(r'\s+', '', (md_disp or '').lower())
                if target_key in (mk_norm, raw_norm, disp_norm):
                    _add_key(md_raw)
                    _add_key(md_disp)
                    _add_key(md_mk)
                    try:
                        for a in json.loads(md_alias_json or "[]"):
                            _add_key(a)
                    except (json.JSONDecodeError, TypeError):
                        pass
            if not matching_keys:
                # fallback: 直接用 display_name
                matching_keys.add(target_key)
            keys_list = list(matching_keys)
            or_clauses = []
            params = [tenant_id]
            for k in keys_list:
                or_clauses.append("REPLACE(LOWER(participant_name),' ','')=?")
                params.append(k)
            sql = ("SELECT participant_name, email, join_time, leave_time, "
                   "duration_minutes, meeting_id, meeting_topic "
                   "FROM official_attendance_sessions "
                   "WHERE tenant_id=? AND (" + " OR ".join(or_clauses) + ") "
                   "ORDER BY join_time")
            rows = conn.execute(sql, params).fetchall()
            # 转成和 zoom_participants 类似的格式
            MYT = timezone(timedelta(hours=8))
            def _fmt_myt(utc_str: str) -> str:
                if not utc_str: return "—"
                try:
                    s = str(utc_str).replace("Z", "+00:00")
                    dt = datetime.fromisoformat(s)
                    return dt.astimezone(MYT).strftime("%m-%d %H:%M:%S")
                except:
                    return str(utc_str)[:16].replace("T", " ")
            def _fmt_dur(sec: int) -> str:
                h, r = divmod(sec, 3600)
                m, s = divmod(r, 60)
                if h: return f"{h}h {m}m"
                return f"{m}m {s}s"
            segments = []
            total_seconds = 0
            for i, r in enumerate(rows):
                join_t = r[2]
                leave_t = r[3]
                dur_raw = r[4]  # duration_minutes
                meeting_id_val = r[5]
                meeting_topic = r[6] or ""
                try:
                    join_dt = datetime.fromisoformat(str(join_t).replace("Z", "+00:00"))
                    leave_dt = datetime.fromisoformat(str(leave_t).replace("Z", "+00:00"))
                    dur_sec = max(0, int((leave_dt - join_dt).total_seconds()))
                except:
                    dur_sec = int((dur_raw or 0) * 60)
                total_seconds += dur_sec
                segments.append({
                    "seq": i + 1,
                    "enter_time": str(join_t),
                    "enter_time_display": _fmt_myt(join_t),
                    "leave_time": str(leave_t),
                    "leave_time_display": _fmt_myt(leave_t),
                    "enter_meeting_id": meeting_id_val,
                    "leave_meeting_id": meeting_id_val,
                    "duration_seconds": dur_sec,
                    "duration_display": _fmt_dur(dur_sec),
                    "status": "⚪️ 已离开",
                })
            return {
                "ok": True,
                "display_name": display_name,
                "join_count": len(segments),
                "leave_count": len(segments),
                "total_seconds": total_seconds,
                "total_display": _fmt_dur(total_seconds),
                "segments": segments,
                "data_source": "history",
            }

        # ── 数据源：实时（zoom_participants）──
        if not current_meeting_id:
            return {"ok": False, "error": "缺少 meeting_id"}
        md_rows = conn.execute(
            "SELECT raw_name, display_name, match_key, aliases FROM member_display WHERE tenant_id=? AND deleted=0",
            (tenant_id,)
        ).fetchall()
        matching_keys = set()
        def _add_key(key: str):
            k = re.sub(r'\s+', '', (key or "").strip().lower())
            if k:
                matching_keys.add(k)
        target_key = re.sub(r'\s+', '', resolved_display.lower())
        _add_key(resolved_display)
        _add_key(raw_match)
        for row in md_rows:
            md_raw, md_disp, md_mk, md_alias_json = row
            mk_norm = re.sub(r'\s+', '', (md_mk or '').lower())
            raw_norm = re.sub(r'\s+', '', (md_raw or '').lower())
            disp_norm = re.sub(r'\s+', '', (md_disp or '').lower())
            if target_key in (mk_norm, raw_norm, disp_norm):
                _add_key(md_raw)
                _add_key(md_disp)
                _add_key(md_mk)
                try:
                    for a in json.loads(md_alias_json or "[]"):
                        _add_key(a)
                except (json.JSONDecodeError, TypeError):
                    pass
        if not matching_keys:
            matching_keys.add(target_key)
        keys_list = list(matching_keys)
        params = [current_meeting_id]
        or_clauses = []
        for k in keys_list:
            or_clauses.append("REPLACE(LOWER(name),' ','')=?")
            params.append(k)
        sql = ("SELECT * FROM zoom_participants WHERE meeting_id=? AND ("
               + " OR ".join(or_clauses) + ")")
        if session_start_after:
            sql += " AND action_time>=?"
            params.append(session_start_after)
        if tenant_id:
            sql += " AND tenant_id=?"
            params.append(tenant_id)
        sql += " ORDER BY action_time"
        rows = conn.execute(sql, params).fetchall()
        raw = [dict(r) for r in rows]
        # ── 同源去重 + 分段逻辑（对齐 Zoom 官方报表口径） ──
        # 规则：同秒 leave→enter 合并；<5秒重复去重；断线5分钟内重连合并为一个 session
        RECONNECT_WINDOW = 300  # 5 分钟
        # Step 1: 标准化 action + 去重 + 同秒合并
        filtered = []
        for ev in raw:
            action = ev.get("action", "")
            if action in ("joined", "enter"):
                action = "enter"
            elif action in ("left", "leave"):
                action = "leave"
            else:
                continue
            try:
                ev_dt = datetime.fromisoformat(str(ev["action_time"]).replace("Z", "+00:00"))
            except:
                ev_dt = datetime.fromisoformat(str(ev["action_time"]))
            if ev_dt.tzinfo is None:
                ev_dt = ev_dt.replace(tzinfo=timezone.utc)
            e = {"action": action, "dt": ev_dt, "raw": ev}
            if not filtered:
                filtered.append(e)
                continue
            prev = filtered[-1]
            gap = (e["dt"] - prev["dt"]).total_seconds()
            # 同秒 leave→enter：合并（breakout 切换）
            if prev["action"] == "leave" and e["action"] == "enter" and abs(gap) <= 2:
                filtered.pop()
                continue
            # 同秒 enter→leave：丢弃 leave（webhook 滞后）
            if prev["action"] == "enter" and e["action"] == "leave" and abs(gap) <= 2:
                continue
            # 连续相同 action <5秒：去重
            if e["action"] == prev["action"] and abs(gap) < 5:
                continue
            filtered.append(e)

        # Step 2: 配对 enter→leave 生成 sessions
        sessions = []
        pending_enter = None
        for ev in filtered:
            if ev["action"] == "enter":
                pending_enter = ev
                continue
            if ev["action"] == "leave" and pending_enter:
                dur = max(0, int((ev["dt"] - pending_enter["dt"]).total_seconds()))
                if dur < 86400:
                    sessions.append({"enter": pending_enter, "leave": ev, "dur": dur})
                pending_enter = None

        # Step 3: 合并断线重连（5分钟内）
        merged_sessions = []
        disconnects = 0
        for s in sessions:
            if not merged_sessions:
                merged_sessions.append(s)
                continue
            prev_s = merged_sessions[-1]
            gap = (s["enter"]["dt"] - prev_s["leave"]["dt"]).total_seconds()
            if 0 < gap <= RECONNECT_WINDOW:
                disconnects += 1
                prev_s["leave"] = s["leave"]
                prev_s["dur"] = (s["leave"]["dt"] - prev_s["enter"]["dt"]).total_seconds()
            else:
                merged_sessions.append(s)

        MYT = timezone(timedelta(hours=8))
        def _fmt_myt(utc_str: str) -> str:
            if not utc_str:
                return "—"
            try:
                s = utc_str.replace("Z", "+00:00")
                dt = datetime.fromisoformat(s)
                return dt.astimezone(MYT).strftime("%m-%d %H:%M:%S")
            except:
                return utc_str[:16].replace("T", " ")
        segments = []
        total_seconds = 0
        now_utc = datetime.now(timezone.utc)
        # 还有未配对的 enter → 在线中
        last_pending = filtered[-1] if filtered and filtered[-1]["action"] == "enter" else None
        for s in merged_sessions:
            dur = int(s["dur"])
            total_seconds += dur
            segments.append({
                "seq": len(segments) + 1,
                "enter_time": s["enter"]["raw"]["action_time"],
                "enter_time_display": _fmt_myt(s["enter"]["raw"]["action_time"]),
                "leave_time": s["leave"]["raw"]["action_time"],
                "leave_time_display": _fmt_myt(s["leave"]["raw"]["action_time"]),
                "enter_meeting_id": s["enter"]["raw"].get("meeting_id", ""),
                "leave_meeting_id": s["leave"]["raw"].get("meeting_id", ""),
                "duration_seconds": dur,
                "duration_display": f"{dur//60}m {dur%60}s" if dur else "0m",
                "status": "⚪️ 已离开",
            })
        # 当前在线中
        if last_pending and last_pending not in [s["leave"] for s in merged_sessions] + [s["enter"] for s in merged_sessions if s["enter"]["dt"] != s["leave"]["dt"]]:
            # 比较复杂——改用简单规则：如果最后一个事件是 enter 且没有对应 leave
            if filtered and filtered[-1]["action"] == "enter":
                last_ev = filtered[-1]
                # 检查这个 enter 是否已经被配对到某个 session
                already_paired = any(s["enter"]["raw"] is last_ev["raw"] or s["enter"]["dt"] == last_ev["dt"] for s in merged_sessions)
                if not already_paired:
                    dur = max(0, int((now_utc - last_ev["dt"]).total_seconds()))
                    total_seconds += dur
                    segments.append({
                        "seq": len(segments) + 1,
                        "enter_time": last_ev["raw"]["action_time"],
                        "enter_time_display": _fmt_myt(last_ev["raw"]["action_time"]),
                        "leave_time": None,
                        "leave_time_display": "在线中",
                        "enter_meeting_id": last_ev["raw"].get("meeting_id", ""),
                        "leave_meeting_id": "",
                        "duration_seconds": dur,
                        "duration_display": f"{dur//60}m {dur%60}s" if dur else "0m",
                        "status": "🟢 在线中",
                    })

        def _fmt_dur(sec: int) -> str:
            h, r = divmod(sec, 3600)
            m, s = divmod(r, 60)
            if h:
                return f"{h}h {m}m"
            return f"{m}m {s}s"
        return {
            "ok": True,
            "display_name": display_name,
            "session_count": len(merged_sessions),
            "disconnect_count": disconnects,
            "total_seconds": total_seconds,
            "total_display": _fmt_dur(total_seconds),
            "segments": segments,
        }

    @app.delete("/api/v3/member-display/{item_id}")
    async def api_v3_member_display_del(item_id: int, request: Request):
        conn = db._get_conn()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        role = request.session.get("role")
        if tenant_id:
            conn.execute("DELETE FROM member_display WHERE id=? AND tenant_id=?", (item_id, tenant_id))
        elif role == "super_admin":
            conn.execute("DELETE FROM member_display WHERE id=?", (item_id,))
        else:
            return {"ok": False, "error": "无权限"}
        conn.commit()
        return {"ok": True}

    @app.delete("/api/v3/members/{display_name}")
    async def api_v3_members_del_by_name(display_name: str, request: Request):
        """按 display_name 软删除成员（标记 deleted=1，保留数据）"""
        conn = db._get_conn()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        role = request.session.get("role")
        if not tenant_id and role == "super_admin":
            tenant_id = request.query_params.get("tenant_id", "").strip()
            if not tenant_id:
                return {"ok": False, "error": "admin 删除需指定 tenant_id 参数"}
        if not tenant_id:
            return {"ok": False, "error": "无法识别租户"}
        if role not in ("super_admin", "admin", "tenant_admin"):
            return {"ok": False, "error": "无权限：需要 admin 以上角色"}

        dn_lower = display_name.strip().lower().replace(" ", "")

        rows = conn.execute(
            "SELECT id FROM member_display WHERE tenant_id=? AND deleted=0 AND "
            "(LOWER(display_name)=? OR LOWER(raw_name)=? OR LOWER(match_key)=?)",
            (tenant_id, dn_lower, dn_lower, dn_lower),
        ).fetchall()
        md_count = len(rows)
        for r in rows:
            conn.execute(
                "UPDATE member_display SET deleted=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (r["id"],),
            )

        conn.commit()

        # ── 如果 member_display 无记录（纯 zoom_participants 名字），插入 tombstone ──
        if md_count == 0:
            mk = display_name.strip().lower().replace(" ", "")
            conn.execute(
                "INSERT INTO member_display (tenant_id, raw_name, display_name, match_key, deleted, group_id) "
                "VALUES (?, ?, ?, ?, 1, NULL)",
                (tenant_id, display_name.strip(), display_name.strip(), mk),
            )
            conn.commit()

        md_count_final = conn.execute(
            "SELECT COUNT(*) FROM member_display WHERE tenant_id=? AND (LOWER(display_name)=? OR LOWER(raw_name)=? OR LOWER(match_key)=?) AND deleted=1",
            (tenant_id, dn_lower, dn_lower, dn_lower),
        ).fetchone()[0]

        return {"ok": True, "deleted": {"member_display": md_count_final}}

    @app.delete("/api/v3/members/display/{item_id}")
    async def api_v3_members_del_by_id(item_id: int, request: Request):
        """按 id 软删除成员（标记 deleted=1，保留数据）"""
        conn = db._get_conn()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        role = request.session.get("role")
        if not tenant_id:
            return {"ok": False, "error": "无法识别租户"}
        if role not in ("super_admin", "admin", "tenant_admin"):
            return {"ok": False, "error": "无权限：需要 admin 以上角色"}

        row = conn.execute(
            "SELECT id FROM member_display WHERE id=? AND tenant_id=? AND deleted=0",
            (item_id, tenant_id),
        ).fetchone()
        if not row:
            return {"ok": False, "error": "记录不存在、不属于当前租户或已删除"}

        conn.execute(
            "UPDATE member_display SET deleted=1, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (item_id,),
        )
        conn.commit()
        return {"ok": True, "deleted": {"member_display": 1}}

    @app.post("/api/v3/members/clean-test-data")
    async def api_v3_members_clean_test(request: Request):
        """批量清理测试成员数据(只删除 member_display, 不碰历史表)"""
        from services.member import MemberService
        tenant_id = request.app.state.get_effective_tenant_id(request)
        if not tenant_id:
            return {"ok": False, "error": "无法识别租户"}
        ms = MemberService()
        result = ms.batch_delete_test_data(tenant_id)
        return result

    @app.get("/members", response_class=HTMLResponse)
    async def members_page(request: Request):
        return tmpl.TemplateResponse(request, "members.html", {"brand": BRAND})

    @app.post("/api/v3/alert/test")
    async def api_v3_alert_test():
        """Test alert: send a sample sharing-timeout alert to Telegram"""
        from telegram_push import send_message
        result = send_message(
            "⚠️ *共享超时告警测试*\n\n"
            "成员:Dino Jun\n"
            "会议:tuijin's Zoom Meeting\n"
            "共享时长:32 分钟\n\n"
        )
        return result

    @app.get("/api/v3/alert/check-sharing")
    async def api_v3_alert_check_sharing():
        """共享告警已临时禁用，等待多租户改造完成"""
        return {"ok": False, "message": "sharing alert disabled until tenant-aware implementation"}
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "demo_mode": settings.demo_mode,
            "time": datetime.now(timezone.utc).isoformat(),
        }

    @app.get("/api/v2/health")
    async def health_v2():
        return {"status": "ok", "version": "2.0.0", "demo_mode": settings.demo_mode}

    # ── OAuth 授权 ────────────────────────────────────────────────────────────
    import secrets
    from urllib.parse import urlencode

    ZOOM_CLIENT_ID = os.environ.get("ZOOM_OAUTH_CLIENT_ID", "")
    ZOOM_CLIENT_SECRET = os.environ.get("ZOOM_OAUTH_CLIENT_SECRET", "")
    REDIRECT_URI = os.environ.get("ZOOM_OAUTH_REDIRECT_URI", "https://zoom.dhbwang.xyz/api/v2/auth/callback")

    def init_oauth_db():
        conn = db._get_conn()
        conn.execute("CREATE TABLE IF NOT EXISTS zoom_oauth_tokens (id INTEGER PRIMARY KEY, account_id TEXT UNIQUE, email TEXT, access_token TEXT, refresh_token TEXT, scope TEXT, expires_at REAL, created_at TEXT)")
        conn.commit()

    @app.get("/api/v2/auth/login")
    async def auth_login(request: Request):
        """生成 Zoom OAuth 授权链接，支持 Web 和 Mobile(Zoom App)两种方式"""
        state = secrets.token_urlsafe(16)
        conn = db._get_conn()
        conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", ("oauth_state", state))
        conn.commit()

        # 通用授权链接
        base_url = "https://zoom.us/oauth/authorize"
        scope_str = os.environ.get("ZOOM_OAUTH_SCOPES", "meeting:read:meeting meeting:read:list_past_participants user:read:user webinar:read:list_past_participants")
        params = urlencode({"response_type":"code","client_id":ZOOM_CLIENT_ID,"redirect_uri":REDIRECT_URI,"state":state,"scope":scope_str})

        # Web 授权
        web_url = f"{base_url}?{params}"

        # 检测是否手机端 - Zoom App 支持 zoomus:// 协议
        ua = request.headers.get("user-agent", "").lower()
        is_mobile = any(k in ua for k in ["mobile", "android", "iphone", "ipad"])

        if is_mobile:
            # 手机端尝试打开 Zoom App
            app_url = f"zoomus://oauth/authorize?{params}"
            return {"ok": True, "auth_url": app_url, "fallback_url": web_url, "is_mobile": True}

        return {"ok": True, "auth_url": web_url, "is_mobile": False}

    @app.get("/api/v2/auth/callback")
    async def auth_callback(code: str = "", state: str = "", error: str = ""):
        """Zoom OAuth 回调 - 用户授权后跳回这里"""
        if error:
            return {"ok": False, "error": f"用户拒绝了授权: {error}"}
        if not code:
            return {"ok": False, "error": "缺少授权码"}
        # 验证 state
        conn = db._get_conn()
        saved_state = conn.execute("SELECT value FROM settings WHERE key='oauth_state'").fetchone()
        if not saved_state or saved_state[0] != state:
            return {"ok": False, "error": "state 不匹配，可能 CSRF 攻击"}
        # 用 code 换 token
        import requests
        r = requests.post("https://zoom.us/oauth/token", data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": REDIRECT_URI,
            "client_id": ZOOM_CLIENT_ID,
            "client_secret": ZOOM_CLIENT_SECRET,
        }, timeout=10)
        d = r.json()
        if "access_token" not in d:
            return {"ok": False, "error": f"获取 token 失败: {d.get('error_description', d)}"}
        # 获取用户信息
        me = requests.get("https://api.zoom.us/v2/users/me", headers={"Authorization": f"Bearer {d['access_token']}"}, timeout=10).json()
        email = me.get("email", "unknown")
        account_id = me.get("account_id", email)
        expires_at = datetime.now(timezone.utc).timestamp() + d.get("expires_in", 3600)
        # 存到数据库
        init_oauth_db()
        conn.execute("INSERT OR REPLACE INTO zoom_oauth_tokens (account_id, email, access_token, refresh_token, scope, expires_at, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (account_id, email, d["access_token"], d.get("refresh_token", ""), d.get("scope", ""), expires_at, datetime.now(timezone.utc).isoformat()))
        conn.commit()
        # 返回成功提示(用一个简单的页面)
        return HTMLResponse("<html><body style='font-family:sans-serif;background:#0f172a;color:#e2e8f0;display:flex;align-items:center;justify-content:center;height:100vh'><div style='text-align:center'><h1>✅ Zoom 授权成功</h1><p>你可以关闭此页面了</p></div></body></html>")

    @app.get("/api/v2/auth/status")
    async def auth_status():
        """查看当前已授权的 Zoom 账号"""
        init_oauth_db()
        conn = db._get_conn()
        rows = conn.execute("SELECT id, account_id, email, scope, expires_at, created_at FROM zoom_oauth_tokens ORDER BY id DESC").fetchall()
        accounts = [{"id": r[0], "account_id": r[1], "email": r[2], "scope": r[3], "expires_at": r[4], "created_at": r[5]} for r in rows]
        return {"ok": True, "accounts": accounts}

    @app.get("/api/v2/auth/refresh/{account_id}")
    async def auth_refresh(account_id: str):
        """刷新指定账号的 token"""
        init_oauth_db()
        conn = db._get_conn()
        row = conn.execute("SELECT access_token, refresh_token FROM zoom_oauth_tokens WHERE account_id=?", (account_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "未找到该账号"}
        import requests
        r = requests.post("https://zoom.us/oauth/token", data={
            "grant_type": "refresh_token",
            "refresh_token": row[1],
            "client_id": ZOOM_CLIENT_ID,
            "client_secret": ZOOM_CLIENT_SECRET,
        }, timeout=10)
        d = r.json()
        if "access_token" not in d:
            return {"ok": False, "error": f"刷新失败: {d.get('error_description', d)}"}
        expires_at = datetime.now(timezone.utc).timestamp() + d.get("expires_in", 3600)
        conn.execute("UPDATE zoom_oauth_tokens SET access_token=?, refresh_token=?, expires_at=? WHERE account_id=?", (d["access_token"], d.get("refresh_token", ""), expires_at, account_id))
        conn.commit()
        return {"ok": True, "message": "token 已刷新"}

    # ── 汇总分析 ────────────────────────────────────────────────────────────
    @app.get("/api/v2/summary")
    async def api_summary():
        """参会汇总统计:在线时长、迟到早退、会议室维度

        分层设计:
          - 配对窗口: 最近 7 天(确保跨 UTC 日 enter/leave 能被配对)
          - 统计窗口: MYT 今日 00:00~24:00
          - 输出: 只在统计窗口内有活动的人
        """
        conn = db._get_conn()
        MYT = timezone(timedelta(hours=8))
        now_utc = datetime.now(timezone.utc)
        now_myt = now_utc.astimezone(MYT)

        # 统计窗口: MYT 今日
        myt_report_start = now_myt.replace(hour=0, minute=0, second=0, microsecond=0)
        myt_report_end = myt_report_start + timedelta(days=1)
        report_start_utc = myt_report_start.astimezone(timezone.utc).isoformat()
        report_end_utc = myt_report_end.astimezone(timezone.utc).isoformat()

        # 配对窗口: 统计窗口前 7 天
        lookup_start = (myt_report_start - timedelta(days=7)).astimezone(timezone.utc).isoformat()

        rows = conn.execute(
            "SELECT * FROM zoom_participants WHERE action_time >= ? AND action_time < ? ORDER BY name, action_time",
            (lookup_start, report_end_utc)
        ).fetchall()
        cols = [c[1] for c in conn.execute("PRAGMA table_info(zoom_participants)").fetchall()]

        # 按人+会议分组，算在线时长
        from collections import defaultdict
        user_sessions = defaultdict(lambda: {"enters": [], "leaves": [], "meeting_ids": set()})
        for r in rows:
            record = dict(zip(cols, r))
            name = record.get("name", "?")
            # 统一显示名(alias 归并)
            _rm = resolve_member(name)
            name = _rm["standard_name"]
            mid = record.get("meeting_id", "?")
            action = record.get("action", "")
            t = record.get("action_time", "")
            if action == "enter":
                user_sessions[(name, mid)]["enters"].append(t)
                user_sessions[(name, mid)]["meeting_ids"].add(mid)
            elif action == "leave":
                user_sessions[(name, mid)]["leaves"].append(t)

        def in_today_range(t: str) -> bool:
            """判断一个 ISO 时间是否在 MYT 今日统计窗口内"""
            try:
                dt = datetime.fromisoformat(t.replace("Z", "+00:00"))
                return report_start_utc <= dt.isoformat() < report_end_utc
            except:
                return False

        # 计算每个人的时长，只输出今日窗口内有活动的人
        members = []
        for (name, mid), sess in user_sessions.items():
            total_secs = 0
            enters = sorted(sess["enters"])
            leaves = sorted(sess["leaves"])

            # 配对 enter/leave，只算与 MYT 今日窗口重叠的部分
            for i in range(min(len(enters), len(leaves))):
                try:
                    et_raw = datetime.fromisoformat(enters[i])
                    lt_raw = datetime.fromisoformat(leaves[i])
                    # 裁剪到今日窗口
                    effective_start = max(et_raw, datetime.fromisoformat(report_start_utc.replace("Z", "+00:00")))
                    effective_end = min(lt_raw, datetime.fromisoformat(report_end_utc.replace("Z", "+00:00")))
                    if effective_end > effective_start:
                        total_secs += (effective_end - effective_start).total_seconds()
                except:
                    pass

            # 未配对的 enter(仍在线)，只算今日窗口内时长
            if len(enters) > len(leaves):
                try:
                    et_raw = datetime.fromisoformat(enters[-1])
                    effective_start = max(et_raw, datetime.fromisoformat(report_start_utc.replace("Z", "+00:00")))
                    effective_end = datetime.fromisoformat(report_end_utc.replace("Z", "+00:00"))
                    if effective_end > effective_start:
                        total_secs += (effective_end - effective_start).total_seconds()
                except:
                    pass

            # 判断此人在今日窗口内是否有活动
            has_today_activity = any(in_today_range(t) for t in enters + leaves)
            has_online_now = len(enters) > len(leaves)
            if not has_today_activity and not has_online_now:
                continue

            minutes = int(total_secs / 60)
            is_late_flag = False
            is_early_leave_flag = False
            if minutes > 0 and enters:
                try:
                    enter_dt = datetime.fromisoformat(enters[0].replace("Z", "+00:00"))
                    enter_myt_hour = enter_dt.astimezone(MYT).hour
                    is_late_flag = enter_myt_hour >= 9
                except:
                    pass
            if minutes > 0 and leaves:
                try:
                    leave_dt = datetime.fromisoformat(leaves[-1].replace("Z", "+00:00"))
                    leave_myt_hour = leave_dt.astimezone(MYT).hour
                    is_early_leave_flag = leave_myt_hour < 17
                except:
                    pass

            members.append({
                "name": name, "meeting_id": mid,
                "enter_time": enters[0] if enters else "",
                "leave_time": leaves[-1] if leaves else "",
                "duration_min": minutes,
                "is_late": is_late_flag,
                "is_early_leave": is_early_leave_flag,
            })

        # 会议室维度
        meetings = defaultdict(lambda: {"count": 0, "names": set(), "first_enter": "99:99", "last_leave": "00:00"})
        for m in members:
            mid = m["meeting_id"]
            meetings[mid]["count"] += 1
            meetings[mid]["names"].add(m["name"])
            if m["enter_time"] and m["enter_time"] < meetings[mid]["first_enter"]:
                meetings[mid]["first_enter"] = m["enter_time"]
            if m["leave_time"] and m["leave_time"] > meetings[mid]["last_leave"]:
                meetings[mid]["last_leave"] = m["leave_time"]

        avg_duration = int(sum(m["duration_min"] for m in members) / len(members)) if members else 0
        stats = {
            "total_participants": len(set(m["name"] for m in members)),
            "total_records": len(members),
            "avg_duration_min": avg_duration,
            "late_count": sum(1 for m in members if m.get("is_late")),
            "early_leave_count": sum(1 for m in members if m.get("is_early_leave")),
        }
        return {"ok": True, "stats": stats, "members": sorted(members, key=lambda x: x["name"]), "meetings": [{"meeting_id": k, "count": v["count"], "unique_people": len(v["names"]), "first_enter": v["first_enter"][:19] if v["first_enter"] != "99:99" else "", "last_leave": v["last_leave"][:19] if v["last_leave"] != "00:00" else ""} for k, v in meetings.items()]}



    @app.get("/api/v3/live")
    async def api_v3_live(request: Request):
        """Business Metrics API 实时在线数据(去重)-- 按当前租户 Zoom 账号查询"""
        zm, _ = _get_tenant_zoom_metrics(request)
        if zm is None:
            return {"ok": True, "data": {"meetings": [], "online_list": [], "total_online": 0}}
        data = await zm.get_live()
        return {"ok": True, "data": data}

    @app.get("/api/v3/dashboard")
    async def api_v3_dashboard(request: Request):
        """Dashboard 概览(兼容前端 /api/v3/dashboard 请求)
        
        通过 ZoomService 统一来源。
        """
        conn = db._get_conn()
        report_start_utc, report_end_utc = myt_day_range_to_utc()
        tenant_id = request.app.state.get_effective_tenant_id(request)
        from services.zoom import ZoomService

        zoom = ZoomService()
        
        online_count = 0
        sharing_count = 0
        meetings = []
        participant_count = 0
        participants = []
        join_count = 0
        leave_count = 0

        # ── 主源: ZoomService get_live_meetings ──
        try:
            live = await zoom.get_live_meetings(tenant_id)
            meetings_data = live.get("meetings", [])
            online_list = live.get("online_list", [])
            online_count = live.get("online_count", 0)

            # Build meetings
            for m in meetings_data:
                meeting_participants = []
                for p in m.get("participants", []):
                    meeting_participants.append({
                        "name": p.get("name", ""),
                        "raw_name": p.get("raw_name", ""),
                        "meeting_id": m.get("meeting_id", ""),
                        "meeting_topic": m.get("meeting_topic", ""),
                        "user_id": p.get("user_id", ""),
                        "join_time": p.get("join_time", ""),
                        "status": "in_meeting",
                        "count_enabled": True,
                        "is_aliased": p.get("is_aliased", False),
                        "email": p.get("email", ""),
                        "online_minutes": p.get("online_minutes", 0),
                        "online_display": p.get("online_display", ""),
                        "join_time_display": p.get("join_time_display", ""),
                    })
                meetings.append({
                    "meeting_id": m.get("meeting_id", ""),
                    "meeting_topic": m.get("meeting_topic", ""),
                    "participants": meeting_participants,
                })

            # Build participants list from online_list
            for p in online_list:
                participants.append({
                    "name": p.get("name", ""),
                    "raw_name": p.get("raw_name", ""),
                    "meeting_id": p.get("meeting_id", ""),
                    "last_action": "in_meeting",
                    "last_active": p.get("join_time", ""),
                    "standard_name": p.get("name", ""),
                    "group_name": "",
                    "status": "在线中",
                    "current_session": "",
                    "session_duration": p.get("online_display", ""),
                    "today_duration": p.get("online_display", ""),
                    "leave_count": 0,
                })

            participant_count = len(participants)
            sharing_count = len({p.get("name", "") for p in online_list if p.get("is_sharing")})
        except Exception:
            # ── Fallback: sharing_live + zoom_participants ──
            try:
                _fb_tenant_id = request.app.state.get_effective_tenant_id(request)
                live_rows = conn.execute(
                    "SELECT meeting_id, user_name, user_id, start_time FROM sharing_live "
                    "WHERE is_active=1 AND tenant_id=? "
                    "AND id IN ("
                    "  SELECT MAX(id) FROM sharing_live "
                    "  WHERE is_active=1 AND tenant_id=? "
                    "  GROUP BY tenant_id, meeting_id, user_name"
                    ")",
                    (_fb_tenant_id, _fb_tenant_id),
                ).fetchall()
                online_count = len({(r["meeting_id"], r["user_id"] or r["user_name"]) for r in live_rows})
                sharing_count = online_count
            except Exception:
                pass

            try:
                participant_count = conn.execute(
                    "SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ?",
                    (report_start_utc, report_end_utc)
                ).fetchone()[0]
            except Exception:
                pass

            try:
                participants_rows = conn.execute(
                    "SELECT name, action, action_time, meeting_id FROM zoom_participants WHERE action_time >= ? AND action_time < ? ORDER BY action_time DESC LIMIT 50",
                    (report_start_utc, report_end_utc)
                ).fetchall()
                seen = set()
                for r in participants_rows:
                    _rm = resolve_member(r["name"])
                    canonical = _rm["standard_name"]
                    if canonical not in seen:
                        seen.add(canonical)
                        participants.append({
                            "name": canonical,
                            "raw_name": r["name"],
                            "meeting_id": r["meeting_id"],
                            "last_action": r["action"],
                            "last_active": r["action_time"],
                        })
            except Exception:
                pass

        # join/leave counts always from DB (historical)
        try:
            join_count = conn.execute(
                "SELECT COUNT(*) FROM zoom_participants WHERE action = 'enter' AND action_time >= ? AND action_time < ?",
                (report_start_utc, report_end_utc)
            ).fetchone()[0]
        except Exception:
            pass

        try:
            leave_count = conn.execute(
                "SELECT COUNT(*) FROM zoom_participants WHERE action = 'leave' AND action_time >= ? AND action_time < ?",
                (report_start_utc, report_end_utc)
            ).fetchone()[0]
        except Exception:
            pass

        return {
            "ok": True,
            "participant_count": participant_count,
            "online_count": online_count,
            "join_count": join_count,
            "leave_count": leave_count,
            "participants": participants,
            "sharing_count": sharing_count,
            "meetings": meetings,
        }

    # ── 多租户总览(super_admin 专用) ────────────────────────────────────

    @app.get("/api/v3/overview")
    async def api_v3_overview(request: Request):
        """跨租户总览:每个租户的在线人数、成员数、账号状态"""
        role = request.session.get("role", "")
        if role != "super_admin":
            return JSONResponse(status_code=403, content={"ok": False, "error": "无权限"})

        from zoom_metrics import ZoomMetrics
        conn = db._get_conn()
        tenants = conn.execute("SELECT * FROM tenants WHERE is_active=1").fetchall()
        result = []

        for t_row in tenants:
            t = dict(t_row)
            tid = t["id"]
            # 成员数
            members = conn.execute("SELECT COUNT(*) FROM member_display WHERE tenant_id=?", (tid,)).fetchone()[0]
            # 账号
            accounts = conn.execute("SELECT * FROM zoom_accounts WHERE tenant_id=? AND is_active=1", (tid,)).fetchall()
            account_info = []
            online_count = 0
            for a_row in accounts:
                a = dict(a_row)
                acct_label = a.get("label", tid)
                acct_status = a.get("status", "unknown")
                # 尝试拉在线
                try:
                    from services.zoom import ZoomService
                    zoom_svc = ZoomService()
                    live = await zoom_svc.get_live_meetings(tenant_id=a.get("tenant_id", tid))
                    acct_online = live.get("online_count", 0)
                    online_count += acct_online
                    account_info.append({
                        "label": acct_label,
                        "status": acct_status,
                        "online": acct_online,
                        "plan": a.get("zoom_plan", "unknown"),
                        "metrics_ok": live.get("online_count", 0) > 0 or True,
                    })
                except Exception as e:
                    account_info.append({
                        "label": acct_label,
                        "status": acct_status,
                        "online": 0,
                        "plan": a.get("zoom_plan", "unknown"),
                        "metrics_ok": False,
                        "error": str(e)[:80],
                    })
            result.append({
                "tenant_id": tid,
                "display_name": t.get("display_name", tid),
                "members": members,
                "accounts": len(accounts),
                "online": online_count,
                "account_info": account_info,
                "plan": t.get("zoom_plan", "unknown"),
                "metrics_available": t.get("metrics_available", 0),
            })

        return {"ok": True, "tenants": result}

    # ── 实时功能 ────────────────────────────────────────────────────────────

    async def _build_live_from_metrics(meetings_data: list, token: str) -> dict:
        """Unified live state builder"""
        import httpx
        from datetime import datetime, timezone, timedelta
        MYT = timezone(timedelta(hours=8))
        now_utc = datetime.now(timezone.utc)
        meetings = {}
        participants_summary = {}
        top_active = {}
        online_count = 0
        async with httpx.AsyncClient(timeout=10) as client:
            for m in meetings_data:
                mid = str(m.get("id", ""))
                topic = m.get("topic", mid)
                raw_count = m.get("participants", 0)
                online_count += raw_count
                start_time = m.get("start_time", "")
                elapsed = 0
                if start_time:
                    try:
                        sd = datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                        elapsed = int((now_utc - sd).total_seconds() / 60)
                    except: pass
                meetings[mid] = {"meeting_id": mid, "topic": topic,
                                 "raw_online_count": raw_count,
                                 "start_time": start_time, "elapsed_minutes": elapsed}
                try:
                    pr = await client.get(
                        "https://api.zoom.us/v2/metrics/meetings/" + mid + "/participants?page_size=300",
                        headers={"Authorization": "Bearer " + token})
                    if pr.status_code == 200:
                        for p in pr.json().get("participants", []):
                            name = p.get("user_name", "").strip()
                            if not name: continue
                            _rm = resolve_member(name)
                            name = _rm["standard_name"]
                            jt = p.get("join_time", "")
                            is_sh = p.get("share_application") or p.get("share_desktop") or p.get("share_whiteboard")
                            sc = "application" if p.get("share_application") else ("desktop" if p.get("share_desktop") else ("whiteboard" if p.get("share_whiteboard") else ""))
                            mins = 0; disp = ""; jtd = ""
                            if jt:
                                try:
                                    jd = datetime.fromisoformat(jt.replace("Z", "+00:00"))
                                    mins = max(0, int((now_utc - jd).total_seconds() / 60))
                                    disp = "{:d}h{:02d}".format(mins // 60, mins % 60) if mins >= 60 else "{}分钟".format(mins)
                                    jtd = jd.astimezone(MYT).strftime("%m-%d %H:%M:%S")
                                except: pass
                            ol = True
                            if jt:
                                try:
                                    jd = datetime.fromisoformat(jt.replace("Z", "+00:00"))
                                    if (now_utc - jd).total_seconds() > 600: ol = False
                                except: pass
                            key = name.lower().replace(" ", "")
                            top_active[name] = top_active.get(name, 0) + 1
                            if key not in participants_summary:
                                participants_summary[key] = {"name": name, "is_online": ol,
                                    "last_active": jt, "last_active_display": jtd,
                                    "total_actions": 0, "duration_display": disp, "flags": [],
                                    "email": p.get("email", ""), "meeting_id": mid,
                                    "is_sharing": is_sh, "share_content": sc,
                                    "standard_name": _rm["standard_name"],
                                    "group_name": _rm["group_name"],
                                    "is_mapped": _rm["is_mapped"]}
                            else:
                                participants_summary[key]["total_actions"] += 1
                                if jt and (not participants_summary[key]["last_active"] or jt > participants_summary[key]["last_active"]):
                                    participants_summary[key]["last_active"] = jt
                                    participants_summary[key]["last_active_display"] = jtd
                                    participants_summary[key]["duration_display"] = disp
                                if is_sh: participants_summary[key]["is_sharing"] = True
                                if not participants_summary[key]["is_online"] and ol:
                                    participants_summary[key]["is_online"] = ol
                except: pass
        ps = list(participants_summary.values())
        ps.sort(key=lambda x: (-x["total_actions"], -len(x.get("flags", []))))
        ol_list = [p for p in ps if p["is_online"]]
        sl_list = [p for p in ol_list if p.get("is_sharing")]
        sa = sorted(top_active.items(), key=lambda x: -x[1])[:3]
        return {
            "ok": True,
            "total_online": len(ol_list),
            "meetings": list(meetings.values()),
            "participants_summary": ps,
            "online_list": ol_list,
            "sharing_list": sl_list,
            "unique_participants": len(ps),
            "top_online": sorted(ol_list, key=lambda x: int(x.get("duration_display","0").replace("h","").replace("分钟","") or 0), reverse=True)[:5],
            "top_active": [{"name": k, "count": v} for k, v in sa],
            "anomalies": [], "health_level": "green",
            "ai_summary": "当前在线 {} 人，{} 个活跃会议室".format(len(ol_list), len(meetings_data)),
        }

    @app.get("/api/v2/live")
    async def api_live():
        """当前在线列表:谁在会议室、在线时长、会议进行状态"""
        # Business Metrics API 优先
        import httpx
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                tr = await c.post("https://zoom.us/oauth/token",
                    data={"grant_type": "account_credentials", "account_id": settings.zoom_account_id},
                    auth=(settings.zoom_client_id, settings.zoom_client_secret))
                if tr.status_code == 200:
                    token = tr.json().get("access_token", "")
                    mr = await c.get("https://api.zoom.us/v2/metrics/meetings?type=live&page_size=100",
                        headers={"Authorization": "Bearer " + token})
                    if mr.status_code == 200:
                        md = mr.json().get("meetings", [])
                        if md:
                            return await _build_live_from_metrics(md, token)
        except:
            pass
        conn = db._get_conn()
        now_utc = datetime.now(timezone.utc)
        rs_utc, re_utc = myt_day_range_to_utc()

        # 找所有今天有 enter 但没有对应 leave 的记录(即还在线上)
        rows = conn.execute("""
            SELECT p1.* FROM zoom_participants p1
            WHERE p1.action_time >= ? AND p1.action = 'enter'
            AND p1.action_time > datetime('now', '-30 minutes')
            AND NOT EXISTS (
                SELECT 1 FROM zoom_participants p2
                WHERE p2.name = p1.name AND p2.meeting_id = p1.meeting_id
                AND p2.action = 'leave' AND p2.action_time > p1.action_time
            )
            ORDER BY p1.action_time DESC
        """, (rs_utc,)).fetchall()
        cols = [c[1] for c in conn.execute("PRAGMA table_info(zoom_participants)").fetchall()]

        now = datetime.now(timezone.utc)
        online = []
        for r in rows:
            d = dict(zip(cols, r))
            try:
                enter_time = datetime.fromisoformat(d["action_time"])
                duration = int((now - enter_time).total_seconds() / 60)
            except:
                duration = 0
            online.append({
                "name": d["name"],
                "meeting_id": d["meeting_id"],
                "enter_time": d["action_time"],
                "online_minutes": duration, "online_display": f"{duration//60}h{duration%60:02d}" if duration >= 60 else f"{duration}分钟",
            })

        # 会议维度:当前有多少会开着
        meetings_online = {}
        for o in online:
            mid = o["meeting_id"]
            if mid not in meetings_online:
                meetings_online[mid] = {"participants": [], "earliest_enter": o["enter_time"]}
            meetings_online[mid]["participants"].append(o["name"])
            if o["enter_time"] < meetings_online[mid]["earliest_enter"]:
                meetings_online[mid]["earliest_enter"] = o["enter_time"]

        # 计算会议已进行时长
        for mid, info in meetings_online.items():
            try:
                start = datetime.fromisoformat(info["earliest_enter"])
                info["elapsed_minutes"] = int((now - start).total_seconds() / 60)
            except:
                info["elapsed_minutes"] = 0
            info["participant_count"] = len(set(info["participants"]))
            del info["participants"]

        # 在线时长排行
        sorted_online = sorted(online, key=lambda x: -x["online_minutes"])
        top_online = [{"name": p["name"], "duration_display": p["online_display"], "minutes": p["online_minutes"]} for p in sorted_online[:5]]

        # 今日总记录数
        total_events = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (rs_utc, re_utc)).fetchone()[0]
        # 唯一参与者数
        unique_participants = conn.execute("SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (rs_utc, re_utc)).fetchone()[0]
        # 在线率 = 当前在线 / 唯一参与者
        unique_online_rate = round(len(online) / unique_participants * 100, 1) if unique_participants > 0 else 0

        # 活跃时段
        hour_dist = conn.execute("SELECT CAST(strftime('%H', action_time) AS INTEGER) as h, COUNT(*) as c FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND action = 'enter' GROUP BY h ORDER BY c DESC LIMIT 3", (rs_utc, re_utc)).fetchall()
        active_hours = [f"{r[0]:02d}:00-{r[0]+1:02d}:00" for r in hour_dist]

        # 最活跃成员
        top_active = conn.execute("SELECT name, COUNT(*) as c FROM zoom_participants WHERE action_time >= ? AND action_time < ? GROUP BY name ORDER BY c DESC LIMIT 3", (rs_utc, re_utc)).fetchall()

        # 参与者汇总(去重后每人统计，含异常检测)
        participants_summary = []
        anomalies = []  # 异常成员列表
        unique_names_rows = conn.execute("SELECT DISTINCT name FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (rs_utc, re_utc)).fetchall()
        for (raw_name,) in unique_names_rows:
            _rm = resolve_member(raw_name)
            name = _rm["standard_name"]
            enters = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND name = ? AND action = 'enter'", (rs_utc, re_utc, raw_name)).fetchone()[0]
            leaves = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND name = ? AND action = 'leave'", (rs_utc, re_utc, raw_name)).fetchone()[0]
            total_actions = enters + leaves
            last_time = conn.execute("SELECT MAX(action_time) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND name = ?", (rs_utc, re_utc, raw_name)).fetchone()[0]
            last_action = conn.execute("SELECT action FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND name = ? ORDER BY action_time DESC LIMIT 1", (rs_utc, re_utc, raw_name)).fetchone()
            is_online = last_action and last_action[0] == "enter"
            # 检查是否在10分钟内活跃
            if is_online and last_time:
                try:
                    last_dt = datetime.fromisoformat(last_time.replace("Z", "+00:00"))
                    now_utc = datetime.now(timezone.utc)
                    if (now_utc - last_dt).total_seconds() > 600:
                        is_online = False
                except:
                    pass

            # 在线时长(前30对进出配对)
            pairs = conn.execute("""
                SELECT e.action_time, l.action_time FROM zoom_participants e
                LEFT JOIN zoom_participants l ON e.rowid < l.rowid AND e.name = l.name AND e.meeting_id = l.meeting_id
                AND e.action = 'enter' AND l.action = 'leave'
                WHERE e.name = ? AND e.action_time >= ?
                ORDER BY e.action_time LIMIT 30
            """, (raw_name, today)).fetchall()
            total_secs = 0; short_sessions = 0
            for et, lt in pairs:
                try:
                    if lt:
                        secs = (datetime.fromisoformat(lt) - datetime.fromisoformat(et)).total_seconds()
                        total_secs += secs
                        if 0 < secs < 60: short_sessions += 1  # 少于1分钟
                except: pass
            duration_min = int(total_secs / 60) if total_secs else 0
            duration_display = f"{duration_min//60}h{duration_min%60:02d}" if duration_min >= 60 else f"{duration_min}分钟"

            # 异常检测
            flags = []
            if total_actions >= 10:
                flags.append("高频进出")
            if enters > 0 and leaves > 0 and short_sessions >= 3:
                flags.append("频繁断线")
            if enters > 0 and duration_min < 1 and short_sessions == 0:
                flags.append("秒进秒出")
            if enters > 0 and duration_min > 0:
                avg_per_session = total_secs / enters / 60
                if avg_per_session < 2:
                    flags.append("在线过短")

            participants_summary.append({
                "name": name, "enters": enters, "leaves": leaves,
                "total_duration_min": duration_min, "duration_display": duration_display,
                "is_online": is_online, "last_active": (last_time or "")[:19], "last_active_display": to_myt_display(last_time or ""),
                "flags": flags, "total_actions": total_actions, "short_sessions": short_sessions,
                "avg_min": round(total_secs / enters / 60, 1) if enters else 0
            })
            if flags:
                anomalies.append({"name": name, "flags": flags, "actions": total_actions})

        # 排序:异常优先，再按时长
        participants_summary.sort(key=lambda x: (-len(x["flags"]), -x["total_duration_min"]))

        # 健康度
        health_level = "green"
        if len(anomalies) >= 3:
            health_level = "red"
        elif len(anomalies) >= 1:
            health_level = "yellow"

        # AI 总结
        ai_summary_parts = [f"今天共有 {unique_participants} 位参与者。"]
        if online:
            ai_summary_parts.append(f"当前在线 {len(online)} 人。")
        if anomalies:
            top_anomaly = anomalies[0]
            if "高频进出" in top_anomaly["flags"]:
                ai_summary_parts.append(f"{top_anomaly['name']} 出现 {top_anomaly['actions']} 次进出，疑似网络不稳定。")
            for a in anomalies[1:2]:
                ai_summary_parts.append(f"{a['name']} {'、'.join(a['flags'])}。")
            ai_summary_parts.append("建议关注:" + "、".join(a["name"] for a in anomalies[:3]))
        else:
            ai_summary_parts.append("整体情况正常。")
        ai_summary = "".join(ai_summary_parts)

        return {"ok": True, "online": online, "meetings": meetings_online, "total_online": len(online),
                "total_events": total_events, "unique_participants": unique_participants,
                "unique_online_rate": unique_online_rate,
                "top_online": top_online, "active_hours": active_hours,
                "top_active": [{"name": r[0], "count": r[1]} for r in top_active],
                "participants_summary": participants_summary,
                "anomalies": anomalies, "health_level": health_level,
                "ai_summary": ai_summary}

    @app.get("/api/v2/attendance")
    async def api_attendance(period: str = "week"):
        """签到统计:周/月维度出勤报表"""
        conn = db._get_conn()
        now = datetime.now(timezone.utc)

        if period == "week":
            start = now - timedelta(days=7)
        elif period == "month":
            start = now - timedelta(days=30)
        else:
            return {"ok": False, "error": "period 必须为 week 或 month"}

        start_str = start.strftime("%Y-%m-%d")
        rows = conn.execute("""
            SELECT name, meeting_id, action, action_time, email
            FROM zoom_participants WHERE action_time >= ?
            ORDER BY name, action_time
        """, (start_str,)).fetchall()
        cols = ["name", "meeting_id", "action", "action_time", "email"]

        from collections import defaultdict
        user_stats = defaultdict(lambda: {"days": set(), "total_entries": 0, "total_duration": 0, "meetings": set(), "email": ""})

        # 先把记录按人分组，配对 enter/leave
        for r in rows:
            d = dict(zip(cols, r))
            name = d["name"]
            user_stats[name]["email"] = d.get("email", "")
            user_stats[name]["meetings"].add(d["meeting_id"])
            if d["action"] == "enter":
                user_stats[name]["total_entries"] += 1
                try:
                    dt = datetime.fromisoformat(d["action_time"])
                    user_stats[name]["days"].add(dt.strftime("%m-%d"))
                except: pass

        # 配对计算时长
        for (name, mid), group in [((r[0], r[1]), list(g)) for r in rows for g in [iter(r)]]:
            pass  # 重新算
        entries_by_user = defaultdict(list)
        for r in rows:
            d = dict(zip(cols, r))
            entries_by_user[d["name"]].append(d)

        result = []
        for name, records in entries_by_user.items():
            total_min = 0
            days = set()
            enters = [r for r in records if r["action"] == "enter"]
            leaves = [r for r in records if r["action"] == "leave"]
            meetings = set(r["meeting_id"] for r in records)
            for e in enters:
                try:
                    dt = datetime.fromisoformat(e["action_time"])
                    days.add(dt.strftime("%m-%d"))
                except: pass
            for i in range(min(len(enters), len(leaves))):
                try:
                    et = datetime.fromisoformat(enters[i]["action_time"])
                    lt = datetime.fromisoformat(leaves[i]["action_time"])
                    total_min += int((lt - et).total_seconds() / 60)
                except: pass
            email = records[0].get("email", "") if records else ""
            result.append({
                "name": name,
                "email": email or "-",
                "total_days": len(days),
                "total_entries": len(enters),
                "total_duration_min": total_min,
                "avg_duration_min": int(total_min / len(enters)) if enters else 0,
                "meeting_count": len(meetings),
            })

        result.sort(key=lambda x: -x["total_duration_min"])
        total_people = len(result)
        total_min_all = sum(r["total_duration_min"] for r in result)

        return {"ok": True, "period": period, "total_people": total_people, "total_minutes": total_min_all, "members": result}

    @app.get("/api/v2/leave-analysis")
    async def api_leave_analysis():
        """离开时长分析:中途离场统计"""
        conn = db._get_conn()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows = conn.execute("""
            SELECT e.name, e.meeting_id, e.action_time as enter_time, l.action_time as leave_time
            FROM zoom_participants e
            JOIN zoom_participants l ON e.name = l.name AND e.meeting_id = l.meeting_id
            WHERE e.action_time >= ? AND e.action = 'enter' AND l.action = 'leave'
            AND l.action_time > e.action_time
            AND l.action_time < datetime(e.action_time, '+4 hours')
            ORDER BY e.name, e.action_time
        """, (today,)).fetchall()

        leaves = []
        total_away = 0
        for r in rows:
            name, mid, et_str, lt_str = r
            try:
                et = datetime.fromisoformat(et_str)
                lt = datetime.fromisoformat(lt_str)
                duration = int((lt - et).total_seconds() / 60)
                if duration > 5 and duration < 240:  # 5分钟以上算离场，4小时内
                    leaves.append({
                        "name": name,
                        "meeting_id": mid,
                        "enter": et_str[11:19],
                        "leave": lt_str[11:19],
                        "duration_min": duration,
                        "type": "短离" if duration < 15 else "中离" if duration < 60 else "长离",
                    })
                    total_away += duration
            except: pass

        # 按人汇总
        from collections import defaultdict
        by_person = defaultdict(list)
        for l in leaves:
            by_person[l["name"]].append(l)

        person_summary = []
        for name, ls in by_person.items():
            person_summary.append({
                "name": name,
                "leave_count": len(ls),
                "total_away_min": sum(l["duration_min"] for l in ls),
                "avg_away_min": int(sum(l["duration_min"] for l in ls) / len(ls)),
                "details": ls[:10],
            })

        person_summary.sort(key=lambda x: -x["total_away_min"])

        return {"ok": True, "total_leaves": len(leaves), "total_away_minutes": total_away, "persons": person_summary}

    @app.get("/api/v2/meetings-auto")
    async def api_meetings_auto():
        """多会议室自动发现:查已安排的会议(非 PMI)"""
        # 先从 zoom_oauth_tokens 找可用的 token(你自己的 Server-to-Server 也行)
        conn = db._get_conn()
        tokens = conn.execute("SELECT access_token FROM zoom_oauth_tokens ORDER BY id DESC LIMIT 1").fetchone()

        import requests as req
        results = {"configured": settings.all_meeting_ids, "auto_discovered": []}

        if tokens:
            try:
                r = req.get("https://api.zoom.us/v2/users/me/meetings?type=scheduled&page_size=30",
                    headers={"Authorization": f"Bearer {tokens[0]}"}, timeout=10)
                d = r.json()
                meetings = d.get("meetings", [])
                for m in meetings:
                    mid = str(m["id"])
                    if mid not in results["configured"] and m.get("type") in (2, 8):  # 已安排会议或定时会议
                        results["auto_discovered"].append({
                            "id": mid,
                            "topic": m.get("topic", ""),
                            "start_time": m.get("start_time", ""),
                            "duration": m.get("duration", 0),
                        })
            except Exception as e:
                results["error"] = str(e)
        else:
            results["note"] = "缺少 OAuth token，先通过面板连接 Zoom 账号"

        return {"ok": True, **results}

    # ── Multi-tenant admin routes ──────────────────────────────────────────
    app.state.compute_kpi_data = _compute_kpi_data

    def _effective_tenant_id(request):
        """统一获取当前请求的有效租户 ID
        super_admin/admin: 走 selected_tenant(可切换)→ 默认 default
        "*" 表示全部租户（返回 None，下游查询不加过滤）
        tenant/viewer: 走 tenant_id(绑定固定)
        """
        role = request.session.get("role", "tenant")
        if role in ("super_admin", "admin"):
            tid = request.session.get("selected_tenant", "default")
            # 防止旧 session 一直停在“所有租户”
            if tid == "*":
                request.session["selected_tenant"] = "default"
                request.session["tenant_id"] = "default"
                return "default"
            return tid or "default"
        return request.session.get("tenant_id", "default")

    app.state.get_effective_tenant_id = _effective_tenant_id

    from admin_routes import router as admin_router
    app.include_router(admin_router, prefix="/dashboard")

    # ── Tenant self-service routes ─────────────────────────────────────────
    from tenant_routes import router as tenant_router
    app.include_router(tenant_router, prefix="/dashboard/tenant")

    # ── Login / Logout ─────────────────────────────────────────────────────
    @app.get("/login", response_class=HTMLResponse)
    async def login_page(request: Request):
        error = request.query_params.get("error", "")
        return tmpl.TemplateResponse(request, "login.html", {"request": request, "error": error})

    @app.post("/login")
    async def login_submit(request: Request, username: str = Form(...), password: str = Form(...)):
        client_ip = get_client_ip(request)

        # ── 登录失败锁定检查 ──
        ip_check = db.check_login_attempts(client_ip)
        if ip_check["locked"]:
            return RedirectResponse(url="/login?error=登录过于频繁，请15分钟后再试", status_code=303)

        user = db.verify_user_password(username, password)
        if not user:
            db.record_login_attempt(client_ip, username, success=False)
            db.log_security_event("login_failed", username=username, ip=client_ip,
                                  user_agent=request.headers.get("user-agent", ""),
                                  result="failed", details="密码错误")
            return RedirectResponse(url="/login?error=用户名或密码错误", status_code=303)
        if not user.get("is_active"):
            db.record_login_attempt(client_ip, username, success=False)
            db.log_security_event("login_failed", username=username, ip=client_ip,
                                  result="failed", details="账号已被禁用")
            return RedirectResponse(url="/login?error=账号已被禁用", status_code=303)

        # ── 密码正确 → 重置尝试计数 ──
        db.record_login_attempt(client_ip, username, success=True)

        # ── 强制 super_admin 开启 2FA ──
        # 已注释：不再强制要求 2FA
        is_super = user.get("role") == "super_admin"
        # if is_super and not db.is_2fa_enabled(user["id"]):
        #     db.log_security_event("login_blocked_no_2fa", user_id=user["id"], username=user["username"],
        #                           tenant_id=user.get("tenant_id", ""), ip=client_ip,
        #                           user_agent=request.headers.get("user-agent", ""),
        #                           result="failed", details="超管必须启用两步验证")
        #     return RedirectResponse(url="/login?error=管理员账户必须启用两步验证，请联系另一管理员", status_code=303)

        # ── Telegram 2FA ──
        if db.is_2fa_enabled(user["id"]):
            import secrets as _secrets
            chat_id = user.get("telegram_chat_id", "")

            # 使用用户所在租户的 bot token，而非全局 token
            user_tenants = db.get_user_tenants(user["id"])
            tenant_id = user_tenants[0]["tenant_id"] if user_tenants else "default"
            bot_cfg = db.get_tenant_bot_config(tenant_id)
            from config import settings as _2fa_settings
            bot_token = bot_cfg.get("token") or _2fa_settings.telegram_bot_token

            code = telegram_push.send_2fa_code(user["id"], chat_id, bot_token=bot_token)
            if code:
                pending_token = _secrets.token_urlsafe(32)
                # 从 _2fa_codes 中取出 chat_id 和 message_id
                _entry = telegram_push.get_2fa_entry(user["id"])
                app.state._2fa_pending[pending_token] = {
                    "user_id": user["id"],
                    "username": user["username"],
                    "role": "super_admin" if user.get("role") == "super_admin" else (user.get("role") or "tenant"),
                    "tenant_id": user.get("tenant_id", "default"),
                    "code": code,
                    "created_at": __import__('time').time(),
                    "ip": client_ip,
                    "chat_id": (_entry or {}).get("chat_id", ""),
                    "message_id": (_entry or {}).get("message_id"),
                }
                return RedirectResponse(url=f"/login/2fa?token={pending_token}", status_code=303)
            else:
                # Telegram delivery failed, allow backup code
                return RedirectResponse(url="/login/2fa?backup=1&uid=" + str(user["id"]), status_code=303)

        # ── 正常登录流程 ──
        db.log_security_event("login_success", user_id=user["id"], username=user["username"],
                              tenant_id=user.get("tenant_id", ""), ip=client_ip,
                              user_agent=request.headers.get("user-agent", ""))

        # ── 新 IP 登录 Telegram 通知 ──
        try:
            last_ip = db.get_last_login_ip(user["id"])
            if last_ip and last_ip != client_ip:
                chat_id = user.get("telegram_chat_id", "")
                if chat_id:
                    ua = request.headers.get("user-agent", "未知")
                    _ua_short = ua[:60] + "…" if len(ua) > 60 else ua
                    msg = (
                        f"🔐 新 IP 登录通知\n\n"
                        f"账号:{user['username']}\n"
                        f"IP:{client_ip}\n"
                        f"时间:{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                        f"设备:{_ua_short}"
                    )
                    telegram_push.send_message(msg, chat_id)
                    db.log_security_event("new_ip_login_notice", user_id=user["id"],
                                          username=user["username"], ip=client_ip,
                                          user_agent=request.headers.get("user-agent", ""),
                                          result="success", details=f"新IP={client_ip}, 上次IP={last_ip}")
        except Exception as _nip_e:
            import logging as _nip_log
            _nip_log.getLogger("app").warning(f"new_ip_login_notice error: {_nip_e}")

        request.session.clear()
        request.session["user_id"] = user["id"]
        request.session["username"] = user["username"]
        request.session["role"] = "super_admin" if user.get("role") == "super_admin" else (user.get("role") or "tenant")

        role = request.session["role"]
        if role == "super_admin":
            # super_admin 默认进入 default，避免一登录就看到全部租户数据
            # 仍然保留后续在租户切换里选择 * 查看全部的能力
            request.session["selected_tenant"] = "default"
            request.session["tenant_id"] = "default"
            return RedirectResponse(url="/dashboard", status_code=303)

        if role == "admin":
            request.session["selected_tenant"] = "default"
            request.session["tenant_id"] = "default"
            return RedirectResponse(url="/dashboard", status_code=303)

        user_tenants = db.get_user_tenants(user["id"])
        if len(user_tenants) == 0:
            # Fallback: use user.tenant_id directly
            tenant_id = user.get("tenant_id", "")
            if tenant_id:
                request.session["selected_tenant"] = tenant_id
                request.session["tenant_id"] = tenant_id
                return RedirectResponse(url="/dashboard/tenant", status_code=303)
            db.log_security_event("login_success_no_tenant", user_id=user["id"], username=user["username"],
                                  ip=client_ip, result="failed", details="未分配租户")
            return RedirectResponse(url="/login?error=未分配任何租户，请联系管理员", status_code=303)
        elif len(user_tenants) == 1:
            selected = user_tenants[0]["tenant_id"]
        else:
            request.session["pending_tenants"] = [t["tenant_id"] for t in user_tenants]
            return RedirectResponse(url="/select-tenant", status_code=303)
        request.session["tenant_id"] = selected
        return RedirectResponse(url="/dashboard/tenant", status_code=303)

    @app.get("/select-tenant")
    async def select_tenant_page(request: Request):
        pending = request.session.get("pending_tenants", [])
        if not pending:
            return RedirectResponse(url="/login", status_code=302)
        tenants = []
        for tid in pending:
            t = db.get_tenant(tid)
            if t:
                tenants.append(t)
        return tmpl.TemplateResponse(request, "select_tenant.html", {"tenants": tenants})

    @app.post("/select-tenant")
    async def select_tenant_submit(request: Request, tenant_id: str = Form(...)):
        pending = request.session.get("pending_tenants", [])
        if tenant_id not in pending:
            return RedirectResponse(url="/select-tenant", status_code=303)
        request.session["tenant_id"] = tenant_id
        del request.session["pending_tenants"]
        role = request.session.get("role", "tenant")
        if role == "super_admin":
            redirect_url = "/dashboard"
        else:
            redirect_url = "/dashboard/tenant"
        return RedirectResponse(url=redirect_url, status_code=303)

    @app.post("/admin/select-tenant")
    async def admin_select_tenant(request: Request,
                                   tenant_id: str = Form(...),
                                   next: str = Form("")):
        """super_admin / admin 切换当前管理的租户
        支持 "所有租户"（*）选项
        """
        role = request.session.get("role", "tenant")
        if role not in ("super_admin", "admin"):
            return RedirectResponse(url="/login", status_code=303)
        # admin 只能切换 default / nolan；只有 super_admin 可以选择 *
        if role == "admin" and tenant_id not in ("default", "nolan"):
            return HTMLResponse("无权限", status_code=403)

        # 校验 tenant 存在（* 代表全部租户，仅 super_admin 可用，跳过校验）
        if tenant_id == "*" and role != "super_admin":
            return HTMLResponse("无权限", status_code=403)
        if tenant_id != "*":
            t = db.get_tenant(tenant_id)
            if not t:
                return HTMLResponse("无效的租户 ID", status_code=400)
        request.session["selected_tenant"] = tenant_id
        request.session["tenant_id"] = tenant_id  # 保持兼容
        redirect_url = next or "/dashboard"
        return RedirectResponse(url=redirect_url, status_code=303)

    # ── Telegram 2FA ───────────────────────────────────────────────────────────
    @app.get("/login/2fa", response_class=HTMLResponse)
    async def login_2fa_page(request: Request, token: str = "", backup: str = "0", uid: str = "", error: str = ""):
        # Validate token exists
        if token and not hasattr(app.state, "_2fa_pending"):
            return RedirectResponse(url="/login", status_code=303)
        if token and token not in getattr(app.state, "_2fa_pending", {}):
            return RedirectResponse(url="/login", status_code=303)
        return tmpl.TemplateResponse(request, "login_2fa.html", {
            "request": request, "token": token, "backup": backup, "uid": uid, "error": error
        })

    @app.post("/login/2fa")
    async def login_2fa_submit(request: Request, token: str = Form(""), code: str = Form(""),
                                backup_code: str = Form(""), uid: str = Form("")):
        import time
        client_ip = get_client_ip(request)
        user_agent = request.headers.get("user-agent", "")

        # ── Verify via pending token (normal 2fa flow) ──
        if token and hasattr(app.state, "_2fa_pending"):
            pending = app.state._2fa_pending.get(token)
            if not pending:
                return RedirectResponse(url="/login", status_code=303)
            if time.time() - pending["created_at"] > 300:
                _chat_id = pending.get("chat_id", "")
                _msg_id = pending.get("message_id")
                if _chat_id and _msg_id is not None:
                    telegram_push.delete_message(str(_chat_id), _msg_id)
                app.state._2fa_pending.pop(token, None)
                return RedirectResponse(url="/login?error=验证码已过期，请重新登录", status_code=303)

            if not telegram_push.verify_2fa_code(pending["user_id"], code):
                return tmpl.TemplateResponse(request, "login_2fa.html", {
                    "request": request, "token": token, "error": "验证码错误"
                })

            # ── 验证成功 → 删除 TG 消息 → 建立 session ──
            # 先删消息(防止 verify_2fa_code pop 后丢失 entry)，再 verify
            _chat_id = pending.get("chat_id", "")
            _msg_id = pending.get("message_id")
            if _chat_id and _msg_id is not None:
                telegram_push.delete_message(str(_chat_id), _msg_id)
            app.state._2fa_pending.pop(token, None)
            db.log_security_event("login_success", user_id=pending["user_id"], username=pending["username"],
                                  tenant_id=pending.get("tenant_id", ""), ip=client_ip,
                                  user_agent=user_agent, details="2FA验证通过")

            # ── 新 IP 登录通知 (2FA 路径) ──
            try:
                _last_ip = db.get_last_login_ip(pending["user_id"])
                if _last_ip and _last_ip != client_ip:
                    _chat = db.get_user_by_id(pending["user_id"]).get("telegram_chat_id", "")
                    if _chat:
                        _msg = (
                            f"🔐 新 IP 登录通知\n\n"
                            f"账号:{pending['username']}\n"
                            f"IP:{client_ip}\n"
                            f"时间:{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"设备:{user_agent[:60] + '…' if len(user_agent) > 60 else user_agent}"
                        )
                        telegram_push.send_message(_msg, _chat)
                        db.log_security_event("new_ip_login_notice", user_id=pending["user_id"],
                                              username=pending["username"], ip=client_ip,
                                              user_agent=user_agent, result="success",
                                              details=f"新IP={client_ip}, 上次IP={_last_ip}")
            except Exception as _e:
                __import__('logging').getLogger("app").warning(f"2fa new_ip notice error: {_e}")

            request.session.clear()
            request.session["user_id"] = pending["user_id"]
            request.session["username"] = pending["username"]
            request.session["role"] = pending["role"]

            role = pending["role"]
            if role == "super_admin":
                request.session["selected_tenant"] = "default"
                request.session["tenant_id"] = "default"
                return RedirectResponse(url="/dashboard", status_code=303)

            user_tenants = db.get_user_tenants(pending["user_id"])
            if len(user_tenants) == 0:
                return RedirectResponse(url="/login?error=未分配任何租户，请联系管理员", status_code=303)
            elif len(user_tenants) == 1:
                selected = user_tenants[0]["tenant_id"]
            else:
                request.session["pending_tenants"] = [t["tenant_id"] for t in user_tenants]
                return RedirectResponse(url="/select-tenant", status_code=303)
            request.session["tenant_id"] = selected
            return RedirectResponse(url="/dashboard/tenant", status_code=303)

        # ── Backup code flow ──
        uid_int = int(uid) if uid and uid.isdigit() else 0
        if uid_int and db.verify_backup_code(uid_int, backup_code):
            user = db.get_user_by_id(uid_int)
            # 删除之前发送的验证码消息(如果有)
            telegram_push.delete_2fa_message(uid_int)
            if not user:
                return RedirectResponse(url="/login?error=用户不存在", status_code=303)
            db.log_security_event("login_success", user_id=user["id"], username=user["username"],
                                  tenant_id=user.get("tenant_id", ""), ip=client_ip,
                                  user_agent=user_agent, details="备用码验证通过")

            # ── 新 IP 登录通知 (备用码路径) ──
            try:
                _last_ip = db.get_last_login_ip(user["id"])
                if _last_ip and _last_ip != client_ip:
                    _chat = user.get("telegram_chat_id", "")
                    if _chat:
                        _msg = (
                            f"🔐 新 IP 登录通知\n\n"
                            f"账号:{user['username']}\n"
                            f"IP:{client_ip}\n"
                            f"时间:{__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                            f"设备:{user_agent[:60] + '…' if len(user_agent) > 60 else user_agent}"
                        )
                        telegram_push.send_message(_msg, _chat)
                        db.log_security_event("new_ip_login_notice", user_id=user["id"],
                                              username=user["username"], ip=client_ip,
                                              user_agent=user_agent, result="success",
                                              details=f"新IP={client_ip}, 上次IP={_last_ip}")
            except Exception as _e:
                __import__('logging').getLogger("app").warning(f"backup new_ip notice error: {_e}")

            request.session.clear()
            request.session["user_id"] = user["id"]
            request.session["username"] = user["username"]
            request.session["role"] = "super_admin" if user.get("role") == "super_admin" else (user.get("role") or "tenant")
            role = request.session["role"]
            if role == "super_admin":
                request.session["selected_tenant"] = "default"
                request.session["tenant_id"] = "default"
                return RedirectResponse(url="/dashboard", status_code=303)
            user_tenants = db.get_user_tenants(user["id"])
            if len(user_tenants) == 0:
                return RedirectResponse(url="/login?error=未分配任何租户，请联系管理员", status_code=303)
            elif len(user_tenants) == 1:
                selected = user_tenants[0]["tenant_id"]
            else:
                request.session["pending_tenants"] = [t["tenant_id"] for t in user_tenants]
                return RedirectResponse(url="/select-tenant", status_code=303)
            request.session["tenant_id"] = selected
            return RedirectResponse(url="/dashboard/tenant", status_code=303)

        return tmpl.TemplateResponse(request, "login_2fa.html", {
            "request": request, "token": token, "backup": "1", "uid": uid, "error": "备用码错误或已使用"
        })

    @app.get("/logout")
    async def logout_get(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    @app.post("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse(url="/login", status_code=303)

    # ── Landing page redirect ──────────────────────────────────────────────
    @app.get("/", response_class=HTMLResponse)
    async def root(request: Request):
        user_id = request.session.get("user_id")
        if user_id and db.get_user_by_id(user_id):
            return RedirectResponse(url="/dashboard", status_code=302)
        return RedirectResponse(url="/login", status_code=302)

    _app = app
    return app


# ── 启动入口 ──────────────────────────────────────────────────────────────────

def start_api():
    import uvicorn
    settings.validate_required()
    app = build_app()
    if settings.demo_mode:
        print("[API] DEMO MODE - 无 Zoom/Telegram 凭据要求")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")


def start_webhook():
    import uvicorn
    if settings.demo_mode:
        print("[WEBHOOK] DEMO MODE - Webhook 不处理真实事件，返回 OK")
        settings.validate_required()
        app = build_app()
        uvicorn.run(app, host=settings.webhook_host, port=settings.webhook_port, log_level="info")
    else:
        settings.validate_required()
        app = build_app()
        uvicorn.run(app, host=settings.webhook_host, port=settings.webhook_port, log_level="info")


def start_monitor():
    if settings.demo_mode:
        print("[MONITOR] DEMO MODE - 跳过 Monitor(无真实 Zoom API 可轮询)")
        import time
        while True:
            time.sleep(60)
            sys.stdout.write("[MONITOR] DEMO 模式运行中...\n")
            sys.stdout.flush()
        return
    settings.validate_required()
    db.init_db()
    from monitor import monitor_loop
    asyncio.run(monitor_loop())


def start_command():
    if settings.demo_mode:
        print("[COMMAND] DEMO MODE - 跳过 Telegram CommandBot(无真实 Bot Token)")
        import time
        while True:
            time.sleep(60)
            sys.stdout.write("[COMMAND] DEMO 模式运行中...\n")
            sys.stdout.flush()
        return
    from command_bot import poll_loop
    from db import init_bot_state
    init_bot_state()
    print("[command] Telegram CommandBot 启动...")
    poll_loop()


if __name__ == "__main__":
    import sys as _sys
    mode = _sys.argv[1] if len(_sys.argv) > 1 else "api"
    if mode == "api":
        start_api()
    elif mode == "webhook":
        start_webhook()
    elif mode == "monitor":
        start_monitor()
    elif mode == "command":
        start_command()
    elif mode == "demo":
        # Demo 模式:启动 api(dashboard) + webhook(mock)，不启动 monitor/command
        import os
        os.environ["DEMO_MODE"] = "true"
        settings.demo_mode = True
        print("[DEMO] Zoom Monitor DEMO 模式启动")
        from fastapi.responses import RedirectResponse
        start_api()
    else:
        print(f"Usage: python app.py [api|webhook|monitor|command|demo]")
        _sys.exit(1)
