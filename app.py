import os
"""
app.py — Zoom 参会监控统一入口
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
import re
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import settings
import db

MYT = timezone(timedelta(hours=8))

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
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except:
        return None


def iso_to_myt_str(s: str, fmt: str = "%m-%d %H:%M:%S") -> str:
    """UTC ISO → MYT 显示字符串"""
    dt = parse_utc_iso(s)
    if dt is None:
        return s[:16] if s else "—"
    return dt.astimezone(MYT).strftime(fmt)



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

# ── Demo 数据函数（惰性导入）────────────────────────────────────────────────
_DEMO_MODULE = None


def _ensure_demo():
    global _DEMO_MODULE
    if _DEMO_MODULE is None:
        import importlib
        _DEMO_MODULE = importlib.import_module("demo_data")
    return _DEMO_MODULE


def resolve_member(raw_name: str) -> dict:
    """解析原始 Zoom 用户名：返回标准名、分组和是否经过映射"""
    resolved = db.resolve_display_name(raw_name)
    standard = resolved["display_name"]
    group_name = db.get_member_group(standard) or db.get_member_group(raw_name)
    is_mapped = standard != raw_name
    return {"raw_name": raw_name, "standard_name": standard, "group_name": group_name, "is_mapped": is_mapped}


def build_app() -> "FastAPI":
    """创建并配置完整的 FastAPI 应用（只在 api/webhook 模式下调用）"""
    global _app
    if _app is not None:
        return _app

    from fastapi import FastAPI, Request, HTTPException, Form
    from fastapi.responses import HTMLResponse, RedirectResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    app = FastAPI(title=BRAND["app_name_zh"], version="2.0.0")

    # ── 认证中间件（必须注册在 SessionMiddleware 之前，使其成为 innermost） ─────
    @app.middleware("http")
    async def _auth_middleware(request: Request, call_next):
        """全局认证中间件 — 未登录用户重定向到 /login"""
        path = request.url.path

        # 白名单：这些路径不需要认证
        public_paths = [
            "/login", "/logout",
            "/privacy", "/terms",
            "/static/",
            "/api/v2/auth/",
            "/api/v3/live",
            "/webhook",
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
        app.add_middleware(SessionMiddleware, secret_key=settings.session_secret, max_age=86400, same_site="lax", https_only=False)
    except ImportError:
        pass
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    tmpl = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    tmpl.env.globals["to_myt"] = to_myt
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
        return response

    # ── 政策页面（用于 Zoom Marketplace 审核） ────────────────────────────────
    @app.get("/privacy", response_class=HTMLResponse)
    async def privacy_page():
        return HTMLResponse("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Privacy Policy — Zoom Attendance Monitor</title>
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
<footer>Zoom Attendance Monitor — dhbwang.xyz</footer>
</body></html>""")

    @app.get("/terms", response_class=HTMLResponse)
    async def terms_page():
        return HTMLResponse("""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Terms of Service — Zoom Attendance Monitor</title>
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
<footer>Zoom Attendance Monitor — dhbwang.xyz</footer>
</body></html>""")

    # ── 多租户 ZoomMetrics 辅助函数 ──────────────────────────────────────────
    def _get_tenant_zoom_metrics(request: Request) -> tuple:
        """根据请求会话中的 tenant_id，获取该租户的 ZoomMetrics 实例。

        Returns:
            (ZoomMetrics | None, tenant_id | None)
            - tenant 用户且拥有活跃 Zoom 账号: (ZoomMetrics(账号), tenant_id)
            - tenant 用户但无活跃账号: (None, tenant_id) — 不再回退全局
            - 非 tenant（super admin / 未登录）: (ZoomMetrics(全局 .env), None)
        """
        user_role = request.session.get("role")
        if user_role == "tenant":
            tenant_id = request.session.get("tenant_id", "default")
            accounts = db.get_zoom_accounts(tenant_id)
            active = next(
                (a for a in accounts if a.get("is_active") and a.get("status") == "active"),
                None,
            )
            if active:
                from zoom_metrics import ZoomMetrics
                return ZoomMetrics(active), tenant_id
            return None, tenant_id  # No active account → 空数据, 无回退
        # 非 tenant：使用全局 .env ZoomMetrics（super admin / 未登录）
        from zoom_metrics import ZoomMetrics
        return ZoomMetrics(), None

    # ── 看板 ─────────────────────────────────────────────────────────────────
    @app.get("/", response_class=RedirectResponse)
    async def landing(request: Request):
        """Landing Page — 重定向到数据看板"""
        return RedirectResponse(url="/dashboard")

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        if settings.demo_mode:
            demo = _ensure_demo()
            demo.seed_demo_data()
            participants = demo.get_demo_participants()
            alerts = demo.get_demo_alerts()
            stats = demo.get_demo_stats()
        else:
            tid = request.session.get("tenant_id")
            participants = dedup_participants(db.get_today_participants(limit=100, tenant_id=tid))
            alerts = db.get_recent_alerts(limit=20, tenant_id=tid)
            stats = {
                "participant_count": len(participants),
                "alert_count": len(alerts),
                "new_face_count": 0,
                "checkin_rate": 0,
            }
        return tmpl.TemplateResponse(request, "dashboard.html", {
            "today": datetime.now(timezone.utc).astimezone(MYT).strftime("%Y-%m-%d"),
            "participants": participants,
            "alerts": alerts,
            "stats": stats,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
            "to_myt": to_myt,
        })

    # ── Demo ──────────────────────────────────────────────────────────────────
    @app.get("/demo", response_class=HTMLResponse)
    async def demo_page(request: Request, tab: str = "overview"):
        """Demo 模式 — 免 Zoom 账号完整体验"""
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

    # ── 生产数据页面 ───────────────────────────────────────────────────────────
    @app.get("/events", response_class=HTMLResponse)
    async def events_page(request: Request):
        if settings.demo_mode:
            events = _ensure_demo().get_demo_events()
        else:
            events = db.get_recent_events(limit=100, tenant_id=request.session.get("tenant_id"))
        return tmpl.TemplateResponse(request, "events.html", {
            "events": events,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
        })

    @app.get("/participants", response_class=HTMLResponse)
    async def participants_page(request: Request):
        return tmpl.TemplateResponse(request, "participants.html", {
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
        })

    @app.get("/alerts", response_class=HTMLResponse)
    async def alerts_page(request: Request):
        if settings.demo_mode:
            alerts = _ensure_demo().get_demo_alerts()
        else:
            alerts = db.get_recent_alerts(limit=100, tenant_id=request.session.get("tenant_id"))
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
            # 今日统计（MYT 边界）
            rs_utc, re_utc = myt_day_range_to_utc()
            today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            total_today = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (rs_utc, re_utc)).fetchone()[0]
            unique_names = conn.execute("SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (rs_utc, re_utc)).fetchone()[0]
            # 最近7天每天统计
            rows = []
            for i in range(7, -1, -1):
                d = (datetime.now(timezone.utc) - timedelta(days=i)).strftime("%Y-%m-%d")
                cnt = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (d, (datetime.fromisoformat(d) + timedelta(days=1)).isoformat())).fetchone()[0]
                uniq = conn.execute("SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ?", (d, (datetime.fromisoformat(d) + timedelta(days=1)).isoformat())).fetchone()[0]
                rows.append({"date": d, "count": cnt, "unique": uniq})
            # 参会排行（总次数）
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
    # 旧路由重定向（向后兼容）
    @app.get("/settings/zoom", response_class=HTMLResponse)
    @app.get("/settings/telegram", response_class=HTMLResponse)
    async def settings_old_redirect():
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/settings/system")

    # ── Telegram Rules API ──────────────────────────────────────────────

    @app.get("/api/v3/telegram-rules")
    async def api_v3_get_telegram_rules():
        rules = db.get_telegram_rules()
        return {"ok": True, "rules": rules}

    @app.post("/api/v3/telegram-rules")
    async def api_v3_create_telegram_rule(request: Request):
        data = await request.json()
        event_type = data.get("event_type", "").strip()
        if not event_type:
            return {"ok": False, "error": "event_type is required"}
        rule_id = db.upsert_telegram_rule(event_type, data)
        return {"ok": True, "id": rule_id}

    @app.put("/api/v3/telegram-rules/{event_type}")
    async def api_v3_update_telegram_rule(event_type: str, request: Request):
        data = await request.json()
        rule_id = db.upsert_telegram_rule(event_type, data)
        return {"ok": True, "id": rule_id}

    @app.delete("/api/v3/telegram-rules/{event_type}")
    async def api_v3_delete_telegram_rule(event_type: str):
        db.delete_telegram_rule(event_type)
        return {"ok": True}

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
            "meeting.participant_admitted": "准入会议",
            "meeting.started": "会议开始",
            "meeting.ended": "会议结束",
            "user.presence_status_updated": "状态更新",
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

    @app.get("/settings/telegram-rules", response_class=HTMLResponse)
    async def settings_telegram_rules_page(request: Request):
        rules = db.get_telegram_rules()
        return tmpl.TemplateResponse(request, "settings_telegram_rules.html", {
            "brand": BRAND,
            "rules": rules,
        })

    # ── Member Groups API ─────────────────────────────────────────────────

    @app.get("/api/v3/member-groups")
    async def api_v3_get_member_groups():
        """获取所有成员分组"""
        groups = db.get_all_groups()
        return {"ok": True, "groups": groups}

    @app.post("/api/v3/member-groups")
    async def api_v3_create_member_group(request: Request):
        """创建成员分组"""
        data = await request.json()
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO member_groups (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (name, description, now, now),
        )
        conn.commit()
        return {"ok": True, "id": cur.lastrowid}

    @app.put("/api/v3/member-groups/{group_id}")
    async def api_v3_update_member_group(group_id: int, request: Request):
        """更新成员分组"""
        data = await request.json()
        name = data.get("name", "").strip()
        description = data.get("description", "").strip()
        if not name:
            return {"ok": False, "error": "name is required"}
        ok = db.update_member_group(group_id, name, description)
        return {"ok": ok}

    @app.delete("/api/v3/member-groups/{group_id}")
    async def api_v3_delete_member_group(group_id: int):
        """删除成员分组"""
        ok = db.delete_member_group(group_id)
        return {"ok": ok}

    @app.post("/api/v3/member-groups/{group_id}/members")
    async def api_v3_add_member(group_id: int, request: Request):
        """向分组添加成员"""
        data = await request.json()
        member_name = data.get("member_name", "").strip()
        if not member_name:
            return {"ok": False, "error": "member_name is required"}
        ok = db.add_member_to_group(group_id, member_name)
        return {"ok": ok}

    @app.delete("/api/v3/member-groups/{group_id}/members/{member_name}")
    async def api_v3_remove_member(group_id: int, member_name: str):
        """从分组移除成员"""
        import urllib.parse
        member_name = urllib.parse.unquote(member_name)
        ok = db.remove_member_from_group(group_id, member_name)
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

        zoom_status = {"webhook_delay_text": "—", "meeting_count": 0, "online_count": 0, "sharing_count": 0}
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

        # Zoom 凭证数据（原 settings_zoom_page）
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

        # Telegram Bot 数据（原 settings_telegram_page）
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
            import requests as req
            r = req.post(f"https://api.telegram.org/bot{token}/sendMessage", json={
                "chat_id": "7922047310", "text": f"🧪 Zoom Monitor 测试消息\n\nTelegram 推送正常 ✅\n发送时间: {now_str} MYT"
            }, timeout=10)
            return {"ok": r.json().get("ok", False), "sent_at": now_str}
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
        return {"ok": True, **result}

    @app.get("/api/report-data")
    async def api_report_data(days: int = 7):
        """报表数据：趋势 + 排行"""
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
        return db.get_today_participants(limit=limit, tenant_id=request.session.get("tenant_id"))

    @app.get("/api/v3/attendance-summary")
    async def api_v3_attendance_summary(request: Request):
        if settings.demo_mode:
            return _ensure_demo().get_demo_attendance_summary()
        try:
            tenant_id = request.session.get("tenant_id")
            result = db.get_today_attendance_summary(tenant_id=tenant_id)
            # If DB has no data but Zoom API has people live, use live data
            if result.get("total_members", 0) == 0:
                zm, _ = _get_tenant_zoom_metrics(request)
                if zm is None:
                    return result
                live_data = await zm.get_live()
                online_list = live_data.get("online_list", [])
                if online_list:
                    now_utc = datetime.now(timezone.utc)
                    members = []
                    for p in online_list:
                        members.append({
                        "standard_name": p.get("name", ""),
                        "group_name": "",
                        "status": "online",
                        "first_join": p.get("join_time", ""),
                        "today_total_seconds": p.get("online_minutes", 0) * 60,
                        "today_total_duration": p.get("online_display", ""),
                        "join_count": 1,
                        "leave_count": 0,
                        "last_activity": p.get("join_time", ""),
                        "last_action": "enter",
                        "raw_events": [],
                        "first_join_display": "",
                        "last_activity_display": "",
                    })
                    return {
                        "ok": True,
                        "total_members": len(members),
                        "online_count": len(members),
                        "offline_count": 0,
                        "total_duration": members[0]["today_total_duration"] if members else "0m",
                        "avg_duration": members[0]["today_total_duration"] if members else "0m",
                        "date": datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d"),
                        "members": members,
                    }
            return result
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/alerts")
    async def api_alerts(request: Request, limit: int = 50):
        if settings.demo_mode:
            return _ensure_demo().get_demo_alerts(limit=limit)
        return db.get_recent_alerts(limit=limit, tenant_id=request.session.get("tenant_id"))

    @app.get("/api/events")
    async def api_events(request: Request, limit: int = 50):
        if settings.demo_mode:
            return _ensure_demo().get_demo_events(limit=limit)
        return db.get_recent_events(limit=limit, tenant_id=request.session.get("tenant_id"))

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
        tenant_id = request.session.get("tenant_id")
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
    async def zoom_webhook(request: Request):
        if settings.demo_mode:
            return {"ok": True, "demo": True}

        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON")

        import hashlib as _hashlib
        import hmac as _hmac
        # Zoom URL Challenge（验证端点）
        event_type = payload.get("event", "")
        sys.stdout.write(f"[WEBHOOK] Received event: {event_type}\n")
        sys.stdout.write(f"[WEBHOOK] Body preview: {body[:300].decode()}\n")
        sys.stdout.flush()
        if event_type == "endpoint.url_validation":
            plain_token = payload.get("payload", {}).get("plainToken", "")
            enc = _hmac.new(settings.zoom_webhook_secret.encode(), plain_token.encode(), _hashlib.sha256).hexdigest()
            sys.stdout.write(f"[WEBHOOK] Challenge OK: pt={plain_token[:10]}... enc={enc[:10]}...\n")
            sys.stdout.flush()
            return {"plainToken": plain_token, "encryptedToken": enc}

        signature = request.headers.get("x-zm-signature", "")
        ts = request.headers.get("x-zm-request-timestamp", "")
        sys.stdout.write(f"[WEBHOOK] Headers: sig={signature[:50]}... ts={ts}\n")
        sys.stdout.write(f"[WEBHOOK] Body for sig: {body[:200]}\n")
        if settings.zoom_webhook_secret and signature:
            ts = request.headers.get("x-zm-request-timestamp", "")
            sys.stdout.write(f"[WEBHOOK] sig check: ts={ts} body_len={len(body)}\n")
            sys.stdout.flush()
            # Zoom 签名: v0=HMAC_SHA256(secret, "v0:" + timestamp + ":" + body)
            msg = f"v0:{ts}:".encode() + body
            expected = hmac.new(settings.zoom_webhook_secret.encode(), msg, _hashlib.sha256).hexdigest()
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

        if "participant_joined" in event_type or "participant_left" in event_type:
            obj = payload.get("payload", {}).get("object", payload.get("object", {}))
            participant = obj.get("participant", {})
            meeting_id = str(obj.get("id", ""))
            if "breakout" in event_type:
                meeting_id = str(payload.get("payload", {}).get("object", {}).get("id", ""))
            name = participant.get("user_name", "").strip()
            email = participant.get("email", "")
            action = "enter" if "joined" in event_type else "leave"
            action_time = datetime.now(timezone.utc)
            if name and action:
                db.save_participant(meeting_id, name, email, action, action_time,
                                    source="webhook", tenant_id=webhook_tenant_id or "unknown")

        # Sharing events
        if "sharing_started" in event_type or "sharing_ended" in event_type:
            obj = payload.get("payload", {}).get("object", payload.get("object", {}))
            participant = obj.get("participant", {})
            meeting_id = str(obj.get("id", ""))
            name = participant.get("user_name", "").strip()
            raw_uid = str(participant.get("user_id", ""))
            # Breakout room events corrupt user_id by appending timestamp
            if "breakout" in event_type and re.search(r"20\d{2}-\d{2}-\d{2}", raw_uid):
                m = re.match(r"^(\d+)", raw_uid)
                user_id = m.group(1) if m else ""
            else:
                user_id = re.sub(r"[^0-9]", "", raw_uid)[:20]
            sd = participant.get("sharing_details", {})
            content = sd.get("content", "")
            dt_str = sd.get("date_time", "")
            conn = db._get_conn()
            if "sharing_started" in event_type:
                conn.execute(
                    "INSERT INTO sharing_live (meeting_id, user_name, user_id, content, start_time, is_active, source, created_at, updated_at) VALUES (?, ?, ?, ?, ?, 1, 'webhook', ?, ?)",
                    (meeting_id, name, user_id, content, dt_str, datetime.now(timezone.utc).isoformat(), datetime.now(timezone.utc).isoformat())
                )
            elif "sharing_ended" in event_type:
                # Mark by meeting_id + user_id, fallback to user_name
                affected = conn.execute(
                    "UPDATE sharing_live SET end_time=?, is_active=0, updated_at=? WHERE meeting_id=? AND user_id=? AND is_active=1",
                    (dt_str, datetime.now(timezone.utc).isoformat(), meeting_id, user_id)
                ).rowcount
                if affected == 0:
                    conn.execute(
                        "UPDATE sharing_live SET end_time=?, is_active=0, updated_at=? WHERE user_name=? AND is_active=1",
                        (dt_str, datetime.now(timezone.utc).isoformat(), name)
                    )
            conn.commit()

        # ── Webhook Telegram Push ──────────────────────────────────────
        try:
            p_conn = db._get_conn()
            rule = p_conn.execute("SELECT enabled FROM alert_rules WHERE rule_type='webhook_event_push'").fetchone()
            if rule and rule[0] == 1:
                from telegram_push import send_message
                import datetime as _dt
                MYT = _dt.timezone(_dt.timedelta(hours=8))
                now_utc = _dt.datetime.now(_dt.timezone.utc)
                now_myt_str = now_utc.astimezone(MYT).strftime("%m-%d %H:%M:%S")

                # 查租户级 bot_token（优先于全局）
                _bot_token = ""
                if webhook_tenant_id:
                    _row = p_conn.execute(
                        "SELECT telegram_bot_token FROM tenants WHERE id=?", (webhook_tenant_id,)
                    ).fetchone()
                    if _row and _row[0]:
                        _bot_token = _row[0]

                # Build dedup key
                obj = payload.get("payload", {}).get("object", payload.get("object", {}))
                participant = obj.get("participant", {})
                pid = str(participant.get("user_id", "")) or str(participant.get("id", ""))
                ename = participant.get("user_name", "").strip()
                sd = participant.get("sharing_details", {})
                sdt = sd.get("date_time", "")
                
                # Determine event type for display
                push_event = "unknown"
                push_icon = "ℹ️"
                push_title = ""
                _rm = resolve_member(ename)
                standard_name = _rm["standard_name"]
                group_name = _rm["group_name"]
                is_mapped = _rm["is_mapped"]
                if "breakout_room" in event_type:
                    # Check for breakout room events first
                    if "participant_joined" in event_type:
                        push_event = "participant_joined_breakout_room"
                        push_icon = "📌"
                        if group_name:
                            push_title = f"加入【{group_name}】分组讨论室"
                        elif is_mapped:
                            push_title = "加入分组讨论室"
                        else:
                            push_title = f"未配置成员 {standard_name} 加入分组讨论室"
                    elif "participant_left" in event_type:
                        push_event = "participant_left_breakout_room"
                        push_icon = "🚪"
                        if group_name:
                            push_title = f"离开【{group_name}】分组讨论室"
                        elif is_mapped:
                            push_title = "离开分组讨论室"
                        else:
                            push_title = f"未配置成员 {standard_name} 离开分组讨论室"
                    elif "sharing_started" in event_type:
                        push_event = "sharing_started"
                        push_icon = "🖥"
                        push_title = "分组讨论室开始共享屏幕"
                    elif "sharing_ended" in event_type:
                        push_event = "sharing_ended"
                        push_icon = "🖥"
                        push_title = "分组讨论室结束共享屏幕"
                elif "participant_joined" in event_type and "waiting_room" not in event_type:
                    push_event = "participant_joined"
                    push_icon = "📌"
                    if group_name:
                        push_title = f"{standard_name} 进入【{group_name}】主会议"
                    else:
                        push_title = "进入主会议"
                elif "participant_left" in event_type:
                    push_event = "participant_left"
                    push_icon = "🚪"
                    push_title = "成员离开会议"
                elif "waiting_room" in event_type and "joined" in event_type:
                    push_event = "waiting_room_joined"
                    push_icon = "⏳"
                    push_title = "有人在等候室"
                elif "admitted" in event_type:
                    push_event = "admitted"
                    push_icon = "✅"
                    push_title = "等候室成员已准入"
                elif "sharing_started" in event_type:
                    push_event = "sharing_started"
                    push_icon = "🖥"
                    push_title = "开始共享屏幕"
                elif "sharing_ended" in event_type:
                    push_event = "sharing_ended"
                    push_icon = "🖥"
                    push_title = "结束共享屏幕"
                
                if push_title and ename:
                    mid = str(obj.get("id", ""))
                    event_ts = sdt or now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    user_key = pid or standard_name.strip().lower().replace(" ", "")
                    dedup_key = "webhook:" + push_event + ":" + mid + ":" + user_key + ":" + event_ts[:16]
                    sys.stderr.write("[PUSH] dedup_key=" + dedup_key + "\n")
                    sys.stderr.flush()

                    # Check rule-based gate before sending
                    if not db.should_send_telegram(push_event):
                        sys.stderr.write(f"[PUSH] {push_event} blocked by rule (should_send_telegram=False)\n")
                        sys.stderr.flush()
                    else:
                        already = p_conn.execute("SELECT 1 FROM alert_sent WHERE alert_key=?", (dedup_key,)).fetchone()
                        if already:
                            sys.stderr.write("[PUSH] duplicate, skipped\n")
                            sys.stderr.flush()
                        else:
                            content_type = sd.get("content", "") if push_event in ("sharing_started", "sharing_ended") else ""
                            extra_line = "\n\uD83D\uDCC4 \u5185\u5BB9: " + content_type if content_type else ""
                            text = push_icon + " *" + push_title + "*\n\n" + "\uD83D\uDC46 " + standard_name + "\n" + "\uD83D\uDD14 \u4F1A\u8BAE: " + mid + "\n" + "\u23F0 " + now_myt_str + extra_line
                            # 解析 target channel(s)
                            _targets = []
                            try:
                                _rule_r = p_conn.execute("SELECT target_channel_id FROM telegram_alert_rules WHERE event_type=?", (push_event,)).fetchone()
                                if _rule_r and _rule_r[0]:
                                    # 指定了特定频道 → 只发该频道
                                    _ch = db.get_telegram_channel_by_id(_rule_r[0])
                                    if _ch and _ch.get("enabled"):
                                        _targets = [_ch["chat_id"]]
                                else:
                                    # 未指定（默认）→ 发到所有启用频道（排除 TJ 群，TJ 群只收 9h 在线预警）
                                    _all = p_conn.execute("SELECT chat_id FROM telegram_channels WHERE enabled=1").fetchall()
                                    _group2 = settings.telegram_group2_chat_id
                                    _targets = [row[0] for row in _all if not _group2 or str(row[0]) != _group2]
                            except:
                                pass
                            if not _targets:
                                _targets = [None]  # 保底走 settings
                            result = {"ok": False, "error": "no targets"}
                            for _cid in _targets:
                                result = send_message(text, chat_id=_cid, bot_token=_bot_token or None)
                                sys.stderr.write(f"[PUSH] send to {_cid}: " + str(result) + "\n")
                                sys.stderr.flush()
                            if result.get("ok"):
                                p_conn.execute("INSERT OR REPLACE INTO alert_sent (alert_key, rule_type, sent_at) VALUES (?, ?, ?)",
                                    (dedup_key, "webhook_event_push", now_utc.isoformat()))
                                p_conn.commit()
                                sys.stderr.write("[PUSH] inserted alert_sent\n")
                                sys.stderr.flush()
                            else:
                                sys.stderr.write("[PUSH] send failed: " + str(result.get("error", "")) + "\n")
                                sys.stderr.flush()
        except Exception as e:
            sys.stderr.write(f"[WEBHOOK_PUSH] error: {e}\n")
            sys.stderr.flush()
        # ────────────────────────────────────────────────────────────────
        
        return {"ok": True}

    # ── 健康检查 ─────────────────────────────────────────────────────────────

    @app.get("/settings/members", response_class=HTMLResponse)
    async def settings_members_page(request: Request):
        return tmpl.TemplateResponse(request, "settings_members.html", {"brand": BRAND})

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
        # 合并到 /settings/telegram-rules
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/settings/telegram-rules")

    @app.get("/api/v3/aliases")
    async def api_v3_aliases():
        """获取所有别名配置"""
        conn = db._get_conn()
        rows = conn.execute("SELECT id, canonical_name, alias_name, count_enabled, note, created_at, updated_at FROM member_aliases ORDER BY canonical_name").fetchall()
        cols = ["id", "canonical_name", "alias_name", "count_enabled", "note", "created_at", "updated_at"]
        return {"ok": True, "aliases": [dict(zip(cols, r)) for r in rows]}

    @app.post("/api/v3/aliases")
    async def api_v3_add_alias(request: Request):
        data = await request.json()
        canonical = data.get("canonical_name", "").strip()
        alias = data.get("alias_name", "").strip()
        if not canonical or not alias:
            return {"ok": False, "error": "参数不完整"}
        count_enabled = data.get("count_enabled", 1)
        note = data.get("note", "")
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = db._get_conn()
        try:
            conn.execute(
                "INSERT INTO member_aliases (canonical_name, alias_name, count_enabled, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (canonical, alias, count_enabled, note, now, now)
            )
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}



    @app.get("/api/v3/member-aliases/discover")
    async def api_v3_member_aliases_discover_alias(request: Request):
        """别名：/api/v3/member-aliases/discover -> 同 /api/v3/aliases/discover"""
        conn = db._get_conn()
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        now_str = datetime.now(timezone.utc).isoformat()
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
    async def api_v3_member_names():
        """返回所有历史用户名（去重）"""
        conn = db._get_conn()
        rows = conn.execute("SELECT DISTINCT name FROM zoom_participants ORDER BY name").fetchall()
        names = [r[0] for r in rows]
        return {"ok": True, "names": names}

    @app.get("/api/v3/aliases/discover")
    async def api_v3_discover(request: Request):
        """自动发现历史 Zoom 用户名，统计出现次数和是否在线"""
        conn = db._get_conn()
        from datetime import datetime, timezone, timedelta
        
        # 统计最近30天的 Zoom 用户名出现次数
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
        now_str = datetime.now(timezone.utc).isoformat()
        rows = conn.execute("""
            SELECT name, COUNT(*) as cnt, MAX(action_time) as last_seen
            FROM zoom_participants
            WHERE action_time >= ? AND action_time < ?
            GROUP BY name
            ORDER BY cnt DESC
        """, (cutoff, now_str)).fetchall()
        
        # 加载已配置的别名（来自 member_aliases 表）
        alias_rows = conn.execute("SELECT alias_name FROM member_aliases").fetchall()
        configured_aliases = set()
        for (alias_name,) in alias_rows:
            configured_aliases.add(alias_name.strip().lower().replace(" ", ""))
        
        # 补充：也排除 member_display 中 aliases JSON 字段里的别名
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
        
        # 当前在线（来自 v3）
        unmapped_set = set()
        zm, _ = _get_tenant_zoom_metrics(request)
        live_data = await zm.get_live() if zm else {"meetings": []}
        # 补充来源：所有 zoom_participants 中出现过的用户名
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
        """一键映射：将 Zoom 用户名映射到标准成员名"""
        data = await request.json()
        zoom_name = data.get("zoom_name", "").strip()
        canonical_name = data.get("canonical_name", "").strip()
        count_enabled = data.get("count_enabled", 1)
        note = data.get("note", "")
        
        if not zoom_name or not canonical_name:
            return {"ok": False, "error": "参数不完整"}
        
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = db._get_conn()
        try:
            conn.execute(
                "INSERT OR IGNORE INTO member_aliases (canonical_name, alias_name, count_enabled, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (canonical_name, zoom_name, count_enabled, note, now, now)
            )
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.delete("/api/v3/aliases/{alias_id}")
    async def api_v3_del_alias(alias_id: int):
        conn = db._get_conn()
        conn.execute("DELETE FROM member_aliases WHERE id=?", (alias_id,))
        conn.commit()
        return {"ok": True}


    @app.get("/api/v3/aliases/duplicates")
    async def api_v3_aliases_duplicates():
        """发现疑似重复的 Zoom 用户名（去空格 / 大小写 / 前4词）"""
        import re
        from datetime import datetime, timezone, timedelta
        from collections import defaultdict
        conn = db._get_conn()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
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
        """批量合并：将一组别名合并到标准名"""
        data = await request.json()
        canonical = (data.get("canonical", "") or "").strip()
        aliases = data.get("aliases", [])
        if not canonical or not aliases:
            return {"ok": False, "error": "参数不完整"}
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = db._get_conn()
        # 确保标准名存在
        exist = conn.execute("SELECT display_name FROM member_display WHERE display_name=?", (canonical,)).fetchone()
        if not exist:
            conn.execute("INSERT INTO member_display (display_name, raw_name, aliases, created_at, updated_at) VALUES (?,?, '[]', ?,?)",
                         (canonical, canonical, now, now))
        cnt = 0
        for alias in aliases:
            if alias == canonical: continue
            conn.execute(
                "INSERT OR IGNORE INTO member_aliases (canonical_name, alias_name, count_enabled, created_at, updated_at) VALUES (?,?,1,?,?)",
                (canonical, alias, now, now))
            cnt += 1
        conn.commit()
        return {"ok": True, "mapped": cnt}


    @app.get("/api/v3/sharing-live")
    async def api_v3_sharing_live(request: Request):
        """共享状态：合并 Metrics API + sharing_live 表 + webhook 事件——按当前租户 Zoom 账号查询"""
        import httpx
        import json as _json
        from datetime import datetime, timezone, timedelta
        MYT = timezone(timedelta(hours=8))
        now_utc = datetime.now(timezone.utc)
        STALE_CUTOFF = timedelta(hours=4)
        
        def to_myt(dt_str):
            if not dt_str: return ""
            try:
                d = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
                return d.astimezone(MYT).strftime("%m-%d %H:%M:%S")
            except: return dt_str[:16]
        
        def mins_between(start_str):
            if not start_str: return 0
            try:
                sd = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                return int((now_utc - sd).total_seconds() / 60)
            except: return 0
        
        def disp(m):
            return f"{m//60}h{m%60:02d}" if m >= 60 else f"{m}分钟"
        
        conn = db._get_conn()
        merged = {}  # normalized_name -> sharing_info
        sources = {"metrics_api": 0, "sharing_live": 0, "webhook": 0}
        
        # Source 1: ZoomMetrics API (cached, deduped, consistent with Dashboard/Live)
        try:
            zm, _ = _get_tenant_zoom_metrics(request)
            live_data = await zm.get_live() if zm else {"meetings": []}
            for m in live_data.get("meetings", []):
                mid = m.get("meeting_id", "")
                for p in m.get("participants", []):
                    if not p.get("is_sharing"):
                        continue
                    raw = p.get("raw_name", "")
                    name = p.get("name", "") or raw
                    norm_key = re.sub(r"\s+", "", name.lower())
                    if not norm_key or norm_key in merged:
                        continue
                    uid = p.get("user_id", "")
                    content = p.get("sharing_content", "desktop")
                    merged[norm_key] = {
                        "name": name, "raw_name": raw,
                        "user_id": uid, "meeting_id": mid,
                        "content": content, "start_time": p.get("join_time", ""),
                        "source": "metrics_api"
                    }
                    sources["metrics_api"] += 1
        except Exception as e:
            sys.stderr.write(f"[sharing-live] ZoomMetrics error: {e}\n")
        
        # Source 2: sharing_live table (is_active=1, not stale)
        live_rows = conn.execute(
            "SELECT * FROM sharing_live WHERE is_active=1 ORDER BY start_time DESC"
        ).fetchall()
        live_cols = [c[1] for c in conn.execute("PRAGMA table_info(sharing_live)").fetchall()]
        RECENT_CUTOFF = timedelta(hours=48)
        for r in live_rows:
            d = dict(zip(live_cols, r))
            uid = d.get("user_id", "")
            start_str = d.get("start_time", "")
            # Stale cutoff: >4h → discard from active; >24h → discard entirely
            if start_str:
                try:
                    sd = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    age = now_utc - sd
                    if age > RECENT_CUTOFF:
                        continue
                    if age > STALE_CUTOFF:
                        # stale but within 24h → keep as recent only
                        raw = d.get("user_name", "")
                        _rm = resolve_member(raw)
                        dn = _rm["standard_name"]
                        norm_key = re.sub(r"\s+", "", dn.lower())
                        if norm_key and norm_key not in merged:
                            merged[norm_key] = {"name": dn, "raw_name": raw, "user_id": uid, "meeting_id": d.get("meeting_id", ""),
                                           "content": d.get("content", ""), "start_time": start_str, "source": "sharing_live",
                                           "_stale": True}
                            sources["sharing_live"] += 1
                        continue
                except: pass
            if uid:
                raw = d.get("user_name", "")
                _rm = resolve_member(raw)
                dn = _rm["standard_name"]
                norm_key = re.sub(r"\s+", "", dn.lower())
                if norm_key and norm_key not in merged:
                    merged[norm_key] = {"name": dn, "raw_name": raw, "user_id": uid, "meeting_id": d.get("meeting_id", ""),
                                   "content": d.get("content", ""), "start_time": start_str, "source": "sharing_live"}
                    sources["sharing_live"] += 1
        
        # Source 3: webhook events — recovery from last 2 hours (no ended received)
        cutoff_2h = (now_utc - timedelta(hours=2)).isoformat()
        events = conn.execute(
            "SELECT payload FROM zoom_events WHERE event_type LIKE '%sharing%' AND created_at >= ? ORDER BY created_at DESC",
            (cutoff_2h,)
        ).fetchall()
        started = {}  # (meeting_id, user_id) -> info
        ended = set()  # (meeting_id, user_id) -> ended
        for (payload_json,) in events:
            try:
                p = _json.loads(payload_json)
                et = p.get("event", "")
                obj = p.get("payload", {}).get("object", p.get("object", {}))
                pt = obj.get("participant", {})
                raw_uid2 = str(pt.get("user_id", ""))
                if "breakout" in et and re.search(r"20\d{2}-\d{2}-\d{2}", raw_uid2):
                    m2 = re.match(r"^(\d+)", raw_uid2)
                    uid = m2.group(1) if m2 else ""
                else:
                    uid = re.sub(r"[^0-9]", "", raw_uid2)[:20]
                raw = pt.get("user_name", "").strip()
                sd = pt.get("sharing_details", {})
                dt_str = sd.get("date_time", "")
                content = sd.get("content", "")
                mid = str(obj.get("id", ""))
                if not uid or not mid:
                    continue
                key = (mid, uid)
                if "started" in et:
                    if key not in started:
                        started[key] = {"raw_name": raw, "content": content, "start_time": dt_str, "meeting_id": mid}
                elif "ended" in et:
                    ended.add(key)
            except: pass
        # Remove ended
        for key in ended:
            started.pop(key, None)
        # Filter stale (>2h) and add
        for key, info in list(started.items()):
            st = info.get("start_time", "")
            if st:
                try:
                    sd = datetime.fromisoformat(st.replace("Z", "+00:00"))
                    if (now_utc - sd).total_seconds() > 7200:
                        started.pop(key, None)
                        continue
                except:
                    pass
            uid = key[1]
            _rm = resolve_member(info["raw_name"])
            dn = _rm["standard_name"]
            norm_key = re.sub(r"\s+", "", dn.lower())
            if uid and norm_key and norm_key not in merged:
                merged[norm_key] = {"name": dn, "raw_name": info["raw_name"], "user_id": uid,
                               "meeting_id": info.get("meeting_id", ""),
                               "content": info.get("content", ""), "start_time": info.get("start_time", ""),
                               "source": "webhook_recovery"}
                sources["webhook_recovery"] = sources.get("webhook_recovery", 0) + 1
        
        # Build output: split into active (current) and recent (stale but <24h, shown as history)
        active = []
        recent = []
        for _key, info in merged.items():
            st = info.get("start_time", "")
            m = max(0, mins_between(st))
            entry = {
                "name": info.get("name", ""),
                "raw_name": info.get("raw_name", ""),
                "user_id": info.get("user_id", ""),
                "meeting_id": info.get("meeting_id", ""),
                "content": info.get("content", ""),
                "start_time": st,
                "start_time_display": to_myt(st),
                "duration_minutes": m,
                "duration_display": disp(m),
                "source": info.get("source", ""),
            }
            if info.get("_stale"):
                recent.append(entry)
            else:
                active.append(entry)
        
        return {
            "ok": True,
            "current": len(active),
            "active": active,
            "recent": recent,
            "recent_total": len(recent),
            "sources": sources,
        }
        
        # Build response
        current_sharing = []
        for uid, info in merged.items():
            st = info.get("start_time", info.get("join_time", ""))
            mins = calc_mins(st)
            current_sharing.append({
                "name": info.get("name", ""),
                "raw_name": info.get("raw_name", ""),
                "user_id": uid,
                "content": info.get("content", ""),
                "start_time": st,
                "start_time_display": to_myt(st),
                "duration_minutes": mins,
                "duration_display": disp_mins(mins),
                "source": info.get("source", ""),
            })
        



    @app.get("/api/v3/sharing-debug")
    async def api_v3_sharing_debug():
        """调试：最近 sharing 事件 + 当前 sharing_live 表"""
        conn = db._get_conn()
        from datetime import datetime, timezone, timedelta
        now_utc = datetime.now(timezone.utc)
        cutoff = (now_utc - timedelta(minutes=30)).isoformat()
        events = conn.execute(
            "SELECT id, event_type, created_at, payload FROM zoom_events WHERE event_type LIKE '%sharing%' AND created_at >= ? ORDER BY created_at DESC LIMIT 20",
            (cutoff,)
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
        
        live_rows = conn.execute("SELECT * FROM sharing_live WHERE is_active=1").fetchall()
        live_cols = [c[1] for c in conn.execute("PRAGMA table_info(sharing_live)").fetchall()]
        active_sharing = [dict(zip(live_cols, r)) for r in live_rows]
        
        # Recovery candidates: started in last 2h without ended
        recovery = []
        for (payload_json,) in conn.execute(
            "SELECT payload FROM zoom_events WHERE event_type LIKE '%sharing%' AND created_at >= ? ORDER BY created_at DESC",
            ((now_utc - timedelta(hours=2)).isoformat(),)
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
        tenant_id = request.session.get("tenant_id")
        from datetime import datetime, timezone, timedelta
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
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
    async def api_v3_members_alias():
        """别名：/api/v3/members -> 同 /api/v3/member-display"""
        conn = db._get_conn()
        rows = conn.execute("SELECT * FROM member_display ORDER BY display_name").fetchall()
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
        return {"ok": True, "items": items}

    @app.post("/api/v3/members")
    async def api_v3_members_add(request: Request):
        """POST /api/v3/members — 创建/更新成员（前端JS调用）"""
        data = await request.json()
        raw_name = data.get("raw_name", "").strip()
        display_name = data.get("display_name", "").strip()
        aliases = data.get("aliases", [])
        group_id = data.get("group_id", None)
        if not raw_name or not display_name:
            return {"ok": False, "error": "raw_name 和 display_name 不能为空"}
        import re
        match_key = re.sub(r'\s+', '', raw_name.lower())
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = db._get_conn()
        try:
            # Check if existing record
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
                    """INSERT INTO member_display (raw_name, display_name, match_key, aliases, group_id, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (raw_name, display_name, match_key, json.dumps(aliases), group_id, now, now)
                )
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.get("/api/v3/member-display")
    async def api_v3_member_display_list():
        """所有显示名映射"""
        conn = db._get_conn()
        rows = conn.execute("SELECT * FROM member_display ORDER BY display_name").fetchall()
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
        return {"ok": True, "items": items}

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
        """设置成员的所属分组"""
        data = await request.json()
        group_id = data.get("group_id", None)
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        try:
            conn.execute(
                "UPDATE member_display SET group_id = ?, updated_at = ? WHERE display_name = ?",
                (group_id, now, display_name)
            )
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    @app.delete("/api/v3/member-display/{item_id}")
    async def api_v3_member_display_del(item_id: int):
        conn = db._get_conn()
        conn.execute("DELETE FROM member_display WHERE id=?", (item_id,))
        conn.commit()
        return {"ok": True}

    @app.delete("/api/v3/members/{display_name}")
    async def api_v3_members_del_by_name(display_name: str):
        """按 display_name 删除成员映射（JS 前端调用）"""
        conn = db._get_conn()
        conn.execute("DELETE FROM member_display WHERE display_name=?", (display_name,))
        conn.commit()
        return {"ok": True}

    @app.get("/members", response_class=HTMLResponse)
    async def members_page(request: Request):
        return tmpl.TemplateResponse(request, "members.html", {"brand": BRAND})

    @app.post("/api/v3/alert/test")
    async def api_v3_alert_test():
        """Test alert: send a sample sharing-timeout alert to Telegram"""
        from telegram_push import send_message
        result = send_message(
            "⚠️ *共享超时告警测试*\n\n"
            "成员：Dino Jun\n"
            "会议：tuijin's Zoom Meeting\n"
            "共享时长：32 分钟\n\n"
        )
        return result

    @app.get("/api/v3/alert/check-sharing")
    async def api_v3_alert_check_sharing():
        """Check current sharing for alerts. Manual trigger."""
        from telegram_push import send_message
        import httpx
        from datetime import datetime, timezone, timedelta
        
        now_utc = datetime.now(timezone.utc)
        conn = db._get_conn()
        
        # Get rule
        rule = conn.execute("SELECT * FROM alert_rules WHERE rule_type='sharing_timeout' AND enabled=1").fetchone()
        if not rule:
            return {"ok": False, "error": "sharing_timeout rule not found or disabled"}
        cols = [c[1] for c in conn.execute("PRAGMA table_info(alert_rules)").fetchall()]
        rule = dict(zip(cols, rule))
        threshold = rule.get("threshold_minutes", 30)
        
        # Get current shared meetings via Metrics API
        alerts_triggered = []
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                tr = await c.post("https://zoom.us/oauth/token",
                    data={"grant_type": "account_credentials", "account_id": settings.zoom_account_id},
                    auth=(settings.zoom_client_id, settings.zoom_client_secret))
                if tr.status_code == 200:
                    token = tr.json().get("access_token", "")
                    mr = await c.get("https://api.zoom.us/v2/metrics/meetings?type=live&page_size=100",
                        headers={"Authorization": f"Bearer {token}"})
                    if mr.status_code == 200:
                        for m in mr.json().get("meetings", []):
                            mid = str(m.get("id", ""))
                            topic = m.get("topic", mid)
                            pr = await c.get(f"https://api.zoom.us/v2/metrics/meetings/{mid}/participants?page_size=300",
                                headers={"Authorization": f"Bearer {token}"})
                            if pr.status_code == 200:
                                for p in pr.json().get("participants", []):
                                    if p.get("status") != "in_meeting": continue
                                    is_sharing = p.get("share_application") or p.get("share_desktop") or p.get("share_whiteboard")
                                    if not is_sharing: continue
                                    name_raw = p.get("user_name", "").strip()
                                    _rm = resolve_member(name_raw)
                                    display_name = _rm["standard_name"]
                                    jt = p.get("join_time", "")
                                    mins = 0
                                    if jt:
                                        try:
                                            jd = datetime.fromisoformat(jt.replace("Z", "+00:00"))
                                            mins = max(0, int((now_utc - jd).total_seconds() / 60))
                                        except: pass
                                    if mins >= threshold:
                                        alert_key = f"sharing_timeout_{name_raw}_{mid}"
                                        already = conn.execute("SELECT 1 FROM alert_sent WHERE alert_key=?", (alert_key,)).fetchone()
                                        if not already:
                                            text = (
                                                "⚠️ *长时间共享*\n\n"
                                                + f"成员：{display_name}\n"
                                                + f"会议：{topic}\n"
                                                + f"共享时长：{mins} 分钟\n\n"
                                            )
                                            result = send_message(text, rule.get("chat_id") or None)
                                            if result.get("ok"):
                                                conn.execute("INSERT OR REPLACE INTO alert_sent (alert_key, rule_type, sent_at) VALUES (?, ?, ?)",
                                                    (alert_key, "sharing_timeout", now_utc.isoformat()))
                                                conn.commit()
                                                alerts_triggered.append({"name": display_name, "minutes": mins, "sent": True})
                                            else:
                                                alerts_triggered.append({"name": display_name, "minutes": mins, "sent": False, "error": result.get("error")})
        except Exception as e:
            return {"ok": False, "error": str(e)}
        
        return {"ok": True, "alerts": alerts_triggered}
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
        """生成 Zoom OAuth 授权链接，支持 Web 和 Mobile（Zoom App）两种方式"""
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
        """Zoom OAuth 回调 — 用户授权后跳回这里"""
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
        # 返回成功提示（用一个简单的页面）
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
        """参会汇总统计：在线时长、迟到早退、会议室维度

        分层设计：
          - 配对窗口: 最近 7 天（确保跨 UTC 日 enter/leave 能被配对）
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
            # 统一显示名（alias 归并）
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

            # 未配对的 enter（仍在线），只算今日窗口内时长
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
        """Business Metrics API 实时在线数据（去重）—— 按当前租户 Zoom 账号查询"""
        zm, _ = _get_tenant_zoom_metrics(request)
        if zm is None:
            return {"ok": True, "data": {"meetings": [], "online_list": [], "total_online": 0}}
        data = await zm.get_live()
        return {"ok": True, "data": data}

    @app.get("/api/v3/dashboard")
    async def api_v3_dashboard(request: Request):
        """Dashboard 概览（兼容前端 /api/v3/dashboard 请求）
        
        主数据源：Zoom Metrics API（与 /api/v3/live 一致）
        备选回退：sharing_live / zoom_participants（当 API 不可用时）
        
        按当前租户的 Zoom 账号查询。
        """
        conn = db._get_conn()
        report_start_utc, report_end_utc = myt_day_range_to_utc()

        online_count = 0
        sharing_count = 0
        meetings = []
        participant_count = 0
        participants = []
        join_count = 0
        leave_count = 0

        # ── 主源：Zoom Metrics API ──
        try:
            zm, _ = _get_tenant_zoom_metrics(request)
            live_data = await zm.get_live() if zm else {"meetings": [], "online_list": [], "total_online": 0}

            online_count = live_data.get("total_online", 0)

            # Build meetings from live API data
            for m in live_data.get("meetings", []):
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
            for p in live_data.get("online_list", []):
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

            # Sharing count from Metrics API (is_sharing flag on each participant)
            # Dedup by name — same person across multiple meetings counted once
            # NOT using sharing_live table as override: stale active entries inflate count
            unique_sharing = {p.get("name", "") for p in live_data.get("online_list", []) if p.get("is_sharing")}
            sharing_count = len(unique_sharing)
        except Exception:
            # ── Fallback: sharing_live + zoom_participants ──
            try:
                live_rows = conn.execute("SELECT meeting_id, user_name, user_id, start_time FROM sharing_live WHERE is_active=1").fetchall()
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
        """当前在线列表：谁在会议室、在线时长、会议进行状态"""
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

        # 找所有今天有 enter 但没有对应 leave 的记录（即还在线上）
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

        # 会议维度：当前有多少会开着
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

        # 参与者汇总（去重后每人统计，含异常检测）
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

            # 在线时长（前30对进出配对）
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

        # 排序：异常优先，再按时长
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
            ai_summary_parts.append("建议关注：" + "、".join(a["name"] for a in anomalies[:3]))
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
        """签到统计：周/月维度出勤报表"""
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
        """离开时长分析：中途离场统计"""
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
        """多会议室自动发现：查已安排的会议（非 PMI）"""
        # 先从 zoom_oauth_tokens 找可用的 token（你自己的 Server-to-Server 也行）
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
        user = db.verify_user_password(username, password)
        if not user:
            return RedirectResponse(url="/login?error=用户名或密码错误", status_code=303)
        if not user.get("is_active"):
            return RedirectResponse(url="/login?error=账号已被禁用", status_code=303)
        request.session["user_id"] = user["id"]
        request.session["username"] = user["username"]
        request.session["role"] = user.get("role") or "tenant"
        request.session["tenant_id"] = user.get("tenant_id") or "default"
        role = request.session["role"]
        # 根据角色决定重定向目标
        if role == "super_admin":
            redirect_url = "/dashboard"
        else:
            redirect_url = "/dashboard/tenant"
        return RedirectResponse(url=redirect_url, status_code=303)

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
        print("[API] DEMO MODE — 无 Zoom/Telegram 凭据要求")
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")


def start_webhook():
    import uvicorn
    if settings.demo_mode:
        print("[WEBHOOK] DEMO MODE — Webhook 不处理真实事件，返回 OK")
        settings.validate_required()
        app = build_app()
        uvicorn.run(app, host=settings.webhook_host, port=settings.webhook_port, log_level="info")
    else:
        settings.validate_required()
        app = build_app()
        uvicorn.run(app, host=settings.webhook_host, port=settings.webhook_port, log_level="info")


def start_monitor():
    if settings.demo_mode:
        print("[MONITOR] DEMO MODE — 跳过 Monitor（无真实 Zoom API 可轮询）")
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
        print("[COMMAND] DEMO MODE — 跳过 Telegram CommandBot（无真实 Bot Token）")
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
        # Demo 模式：启动 api（dashboard） + webhook（mock），不启动 monitor/command
        import os
        os.environ["DEMO_MODE"] = "true"
        settings.demo_mode = True
        print("[DEMO] Zoom Monitor DEMO 模式启动")
        from fastapi.responses import RedirectResponse
        start_api()
    else:
        print(f"Usage: python app.py [api|webhook|monitor|command|demo]")
        _sys.exit(1)
