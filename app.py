import re
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
import sys
import time
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


def build_participant_summary(rows):
    """按人聚合原始 zoom_participants 记录，返回 participants 页面所需字段

    跨天配对策略：取最近 7 天数据做配对，但只输出今日有活动的人。
    逻辑复用 /api/v2/summary 的配对方式。
    """
    from collections import defaultdict

    now_utc = datetime.now(timezone.utc)
    MYT = timezone(timedelta(hours=8))
    now_myt = now_utc.astimezone(MYT)
    myt_start = now_myt.replace(hour=0, minute=0, second=0, microsecond=0)
    myt_end = myt_start + timedelta(days=1)
    today_start_utc = myt_start.astimezone(timezone.utc).isoformat()
    today_end_utc = myt_end.astimezone(timezone.utc).isoformat()

    # 取更多数据做配对（向前 7 天）
    lookup_start = (myt_start - timedelta(days=7)).astimezone(timezone.utc).isoformat()
    conn = db._get_conn()
    extra_rows = conn.execute(
        "SELECT * FROM zoom_participants WHERE action_time >= ? AND action_time < ? ORDER BY name, action_time",
        (lookup_start, today_end_utc)
    ).fetchall()
    cols = [c[1] for c in conn.execute("PRAGMA table_info(zoom_participants)").fetchall()]

    if not extra_rows:
        return []

    # 按人+会议分组（复用 api summary 逻辑）
    user_sessions = defaultdict(lambda: {"enters": [], "leaves": []})
    for r in extra_rows:
        record = dict(zip(cols, r))
        name = record.get("name", "?")
        resolved = db.resolve_display_name(name)
        canonical = resolved["display_name"]
        mid = record.get("meeting_id", "?")
        action = record.get("action", "")
        t = record.get("action_time", "")
        if action == "enter":
            user_sessions[(canonical, mid)]["enters"].append(t)
        elif action == "leave":
            user_sessions[(canonical, mid)]["leaves"].append(t)

    def in_today(t):
        try:
            d = datetime.fromisoformat(t.replace("Z", "+00:00"))
            return today_start_utc <= d.isoformat() < today_end_utc
        except:
            return False

    def secs_in_today(start_iso, end_iso):
        try:
            s = max(datetime.fromisoformat(start_iso.replace("Z", "+00:00")),
                    datetime.fromisoformat(today_start_utc.replace("Z", "+00:00")))
            e = min(datetime.fromisoformat(end_iso.replace("Z", "+00:00")),
                    datetime.fromisoformat(today_end_utc.replace("Z", "+00:00")))
            if e > s:
                return int((e - s).total_seconds())
        except:
            pass
        return 0

    def fmt_minutes(secs):
        if secs <= 0:
            return "—"
        h = secs // 3600
        m = (secs % 3600) // 60
        if h > 0:
            return f"{h}h{m}m"
        return f"{m}m"

    # 按 canonical_name 汇总 — 状态机配对算法
    # 每人同一时刻只能有一个 open_enter
    # 重复 enter 不重复计时
    # 未配对 enter 只有当前真的在线且在今天才计入
    person_summary = defaultdict(lambda: {
        "total_secs": 0, "current_secs": 0,
        "first_enter": "", "last_active": "",
        "has_today_activity": False, "is_online": False,
        "leave_count": 0
    })

    for (canonical, mid), sess in user_sessions.items():
        events = []
        for t in sess["enters"]:
            events.append((t, "enter"))
        for t in sess["leaves"]:
            events.append((t, "leave"))
        events.sort(key=lambda x: x[0])

        p = person_summary[canonical]
        open_enter = None

        for t, action in events:
            if action == "enter":
                if open_enter is None:
                    open_enter = t
                    if not p["first_enter"] or t < p["first_enter"]:
                        p["first_enter"] = t
                # 重复 enter，忽略
            elif action == "leave":
                p["leave_count"] += 1
                if in_today(t):
                    p["has_today_activity"] = True
                if open_enter is not None:
                    s = secs_in_today(open_enter, t)
                    p["total_secs"] += s
                    open_enter = None
                # 无 enter 的 leave 不贡献时长

        # 最后活动时间
        if events:
            last_t = events[-1][0]
            if last_t > p["last_active"]:
                p["last_active"] = last_t

        # 未配对 enter
        if open_enter is not None:
            p["is_online"] = True
            if in_today(open_enter):
                p["current_secs"] = secs_in_today(open_enter, now_utc.isoformat())
                p["total_secs"] += p["current_secs"]
        else:
            p["is_online"] = False

    # 构建输出，只保留今日有活动的人
    result = []
    for canonical, p in person_summary.items():
        if not p["has_today_activity"]:
            continue

        result.append({
            "name": canonical,
            "status": "在线中" if p["is_online"] else "已离线",
            "first_enter": p["first_enter"],
            "current_session": fmt_minutes(p["current_secs"]),
            "total_duration": fmt_minutes(p["total_secs"]),
            "leave_count": p.get("leave_count", 0),
            "last_active": p["last_active"],
            "is_online": p["is_online"],
            "action_time": p["first_enter"],  # 兼容模板中 {{ to_myt(p.action_time) }}
        })

    result.sort(key=lambda x: x.get("last_active", ""), reverse=True)
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


def build_app() -> "FastAPI":
    """创建并配置完整的 FastAPI 应用（只在 api/webhook 模式下调用）"""
    global _app
    if _app is not None:
        return _app

    from fastapi import FastAPI, Request, HTTPException
    from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    app = FastAPI(title=BRAND["app_name_zh"], version="2.0.0")
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    tmpl = Jinja2Templates(directory=str(BASE_DIR / "templates"))
    tmpl.env.globals["to_myt"] = to_myt
    tmpl.env.filters["myt"] = to_myt

    # ── LIVE_CACHE: 唯一在线状态缓存（由 /api/v2/live 刷新，dashboard/sharing 只读） ─
    LIVE_CACHE = {
        "ts": 0.0,
        "data": {
            "total_online": 0,
            "online_list": [],
            "online": [],
            "meetings": [],
            "participants_summary": [],
        }
    }

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
            participants = dedup_participants(db.get_today_participants(limit=100))
            alerts = db.get_recent_alerts(limit=20)
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
            events = db.get_recent_events(limit=100)
        return tmpl.TemplateResponse(request, "events.html", {
            "events": events,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
        })

    @app.get("/participants", response_class=HTMLResponse)
    async def participants_page(request: Request, date: str = ""):
        if settings.demo_mode:
            participants = _ensure_demo().get_demo_participants()
        else:
            if date:
                # 指定日期：查该日 MYT 范围
                _d = datetime.strptime(date, "%Y-%m-%d").replace(tzinfo=MYT)
                _ds, _de = myt_day_range_to_utc(_d)
                _conn = db._get_conn()
                _myrows = _conn.execute(
                    "SELECT * FROM zoom_participants WHERE action_time >= ? AND action_time < ? ORDER BY action_time",
                    (_ds, _de)
                ).fetchall()
                _cols = [c2[1] for c2 in _conn.execute("PRAGMA table_info(zoom_participants)").fetchall()]
                _mydicts = [dict(zip(_cols, r)) for r in _myrows]
                participants = build_participant_summary(_mydicts)
            else:
                rows = db.get_today_participants(limit=500)
                participants = build_participant_summary(rows)
            # Live API 覆盖在线状态：只有 live 确认在线的人才标记在线，其余全部离线
            try:
                import urllib.request, json as _json
                live_req = urllib.request.Request("http://localhost:8000/api/v2/live", method="GET")
                with urllib.request.urlopen(live_req, timeout=5) as resp:
                    live = _json.loads(resp.read())
                    online_set = set()
                    for p in live.get("online_list", []):
                        rn = db.resolve_display_name(p.get("name", ""))
                        online_set.add(rn["display_name"])
            except Exception:
                online_set = set()
                import logging
                logging.getLogger("zoom").exception("live API 调用失败，所有参与者标记为离线")
            for p2 in participants:
                p2["is_online"] = p2["name"] in online_set
                p2["status"] = "\u5728\u7ebf\u4e2d" if p2["is_online"] else "\u5df2\u79bb\u7ebf"
        return tmpl.TemplateResponse(request, "participants.html", {
            "participants": participants,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
            "to_myt": to_myt,
            "selected_date": date,
        })

    @app.get("/alerts", response_class=HTMLResponse)
    async def alerts_page(request: Request):
        if settings.demo_mode:
            alerts = _ensure_demo().get_demo_alerts()
        else:
            alerts = db.get_recent_alerts(limit=100)
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

    # ── AI 报告 ────────────────────────────────────────────────────────────
    @app.get("/ai/daily", response_class=HTMLResponse)
    async def ai_daily_page(request: Request):
        return tmpl.TemplateResponse(request, "ai_report.html", {"brand": BRAND, "period": "daily", "period_name": "日报"})

    @app.get("/ai/weekly", response_class=HTMLResponse)
    async def ai_weekly_page(request: Request):
        return tmpl.TemplateResponse(request, "ai_report.html", {"brand": BRAND, "period": "weekly", "period_name": "周报"})

    @app.get("/ai/monthly", response_class=HTMLResponse)
    async def ai_monthly_page(request: Request):
        return tmpl.TemplateResponse(request, "ai_report.html", {"brand": BRAND, "period": "monthly", "period_name": "月报"})

    @app.get("/api/ai-report")
    async def api_ai_report(period: str = "daily"):
        """AI 报告：按周期生成参会分析报告"""
        now = datetime.now(timezone.utc)
        if period == "daily":
            since = now - timedelta(days=1)
        elif period == "weekly":
            since = now - timedelta(days=7)
        elif period == "monthly":
            since = now - timedelta(days=30)
        else:
            return {"ok": False, "error": "period must be daily/weekly/monthly"}
        since_str = since.strftime("%Y-%m-%d")
        conn = db._get_conn()
        rows = conn.execute("SELECT name, action, action_time, meeting_id FROM zoom_participants WHERE action_time >= ? ORDER BY action_time", (since_str,)).fetchall()
        if not rows:
            return {"ok": False, "note": f"该周期内无数据", "period": period}
        names = set()
        entries = 0
        for r in rows:
            names.add(r[0])
            if r[1] == "enter":
                entries += 1
        prompt = f"Zoom 参会监控{period}报告：周期内 {len(names)} 人参会，{entries} 次进入。生成 200 字中文总结。"
        try:
            import requests as req
            ai = req.post("https://sub2api.dhbwang.xyz/v1/chat/completions",
                json={"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 500},
                headers={"Authorization": "Bearer " + (os.environ.get("SUB2API_KEY") or os.environ.get("DEEPSEEK_API_KEY", ""))},
                timeout=30)
            d = ai.json()
            report = d["choices"][0]["message"]["content"] if d.get("choices") else "AI 暂时不可用"
        except Exception as e:
            report = f"AI 不可用: {e}"
        return {"ok": True, "period": period, "total_people": len(names), "total_entries": entries, "report": report}

    # ── 设置 ────────────────────────────────────────────────────────────────
    @app.get("/settings/zoom", response_class=HTMLResponse)
    async def settings_zoom_page(request: Request):
        # 收集 Zoom 配置状态
        conn = db._get_conn()
        tokens = conn.execute("SELECT email, scope, expires_at FROM zoom_oauth_tokens ORDER BY id DESC LIMIT 3").fetchall()
        webhook_status = "未配置"
        try:
            import requests as req
            h = req.get("http://127.0.0.1:9000/health", timeout=3)
            webhook_status = "运行中" if h.status_code == 200 else "异常"
        except: webhook_status = "未启动"
        return tmpl.TemplateResponse(request, "settings_zoom.html", {
            "brand": BRAND,
            "monitor_interval": getattr(settings, "monitor_interval", 300),
            "pmi_id": getattr(settings, "zoom_pmi_id", ""),
            "extra_ids": getattr(settings, "zoom_extra_meeting_ids", ""),
            "host_email": getattr(settings, "zoom_host_email", ""),
            "webhook_status": webhook_status,
            "oauth_accounts": [{"email": r[0], "scope": (r[1] or "")[:50], "expires_at": r[2]} for r in tokens],
        })

    @app.get("/settings/telegram", response_class=HTMLResponse)
    async def settings_telegram_page(request: Request):
        # 检查 bot 状态
        bot_ok = False
        bot_username = ""
        try:
            import requests as req
            token = "8791140288:AAHL_7Az6vQitTIJUhlP-M8YaMXzPz2joG4"
            r = req.get(f"https://api.telegram.org/bot{token}/getMe", timeout=5)
            if r.json().get("ok"):
                bot_ok = True
                bot_username = r.json()["result"]["username"]
        except: pass
        return tmpl.TemplateResponse(request, "settings_telegram.html", {
            "brand": BRAND,
            "bot_ok": bot_ok,
            "bot_username": bot_username,
            "home_chat_id": "7922047310",
        })

    @app.get("/settings/system", response_class=HTMLResponse)
    async def settings_system_page(request: Request):
        import os
        import subprocess
        docker_status = {}
        for name in ["zoom-monitor", "zoom-api", "zoom-webhook", "zoom-command"]:
            try:
                r = subprocess.run(["docker", "inspect", name, "--format", "{{.State.Status}}"], capture_output=True, text=True, timeout=5)
                docker_status[name] = r.stdout.strip()
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

        return tmpl.TemplateResponse(request, "settings_system.html", {
            "brand": BRAND,
            "version": "0.2.1",
            "docker_status": docker_status,
            "zoom_status": zoom_status,
            "participant_count": db.get_today_participants(limit=1) and len(db.get_today_participants(limit=10000)) or 0,
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
            tg = req.get("https://api.telegram.org/bot8791140288:AAHL_7Az6vQitTIJUhlP-M8YaMXzPz2joG4/getMe", timeout=5)
            result["telegram"] = "ok" if tg.json().get("ok") else "error"
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
    async def api_participants(limit: int = 200):
        if settings.demo_mode:
            return _ensure_demo().get_demo_participants(limit=limit)
        return db.get_today_participants(limit=limit)

    @app.get("/api/alerts")
    async def api_alerts(limit: int = 50):
        if settings.demo_mode:
            return _ensure_demo().get_demo_alerts(limit=limit)
        return db.get_recent_alerts(limit=limit)

    @app.get("/api/events")
    async def api_events(limit: int = 50):
        if settings.demo_mode:
            return _ensure_demo().get_demo_events(limit=limit)
        return db.get_recent_events(limit=limit)

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
    async def api_stats():
        if settings.demo_mode:
            return _ensure_demo().get_demo_stats()
        participants = db.get_today_participants(limit=200)
        alerts = db.get_recent_alerts(limit=50)
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
        # Zoom URL Challenge（验证端点）
        event_type = payload.get("event", "")
        sys.stdout.write(f"[WEBHOOK] Received event: {event_type}\n")
        sys.stdout.write(f"[WEBHOOK] Body preview: {body[:300].decode()}\n")
        sys.stdout.flush()
        if event_type == "endpoint.url_validation":
            plain_token = payload.get("payload", {}).get("plainToken", "")
            enc = _hashlib.sha256((settings.zoom_webhook_secret + plain_token).encode()).hexdigest()
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
        db.save_webhook_event(event_type, payload)
        sys.stdout.write(f"[WEBHOOK] {event_type}\n")
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
                                    source="webhook")

        # Sharing events
        if "sharing_started" in event_type or "sharing_ended" in event_type:
            obj = payload.get("payload", {}).get("object", payload.get("object", {}))
            participant = obj.get("participant", {})
            meeting_id = str(obj.get("id", ""))
            name = participant.get("user_name", "").strip()
            user_id = re.sub(r"[^0-9]", "", str(participant.get("user_id", "")))[:20]
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
                if "participant_joined" in event_type and "waiting_room" not in event_type:
                    push_event = "participant_joined"
                    push_icon = "📌"
                    push_title = "会议有新人加入"
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
                    user_key = pid or ename.strip().lower().replace(" ", "")
                    dedup_key = "webhook:" + push_event + ":" + mid + ":" + user_key + ":" + event_ts[:16]
                    sys.stderr.write("[PUSH] dedup_key=" + dedup_key + "\n")
                    sys.stderr.flush()
                    already = p_conn.execute("SELECT 1 FROM alert_sent WHERE alert_key=?", (dedup_key,)).fetchone()
                    if already:
                        sys.stderr.write("[PUSH] duplicate, skipped\n")
                        sys.stderr.flush()
                    else:
                        content_type = sd.get("content", "") if push_event in ("sharing_started", "sharing_ended") else ""
                        extra_line = "\n\uD83D\uDCC4 \u5185\u5BB9: " + content_type if content_type else ""
                        text = push_icon + " *" + push_title + "*\n\n" + "\uD83D\uDC46 " + ename + "\n" + "\uD83D\uDD14 \u4F1A\u8BAE: " + mid + "\n" + "\u23F0 " + now_myt_str + extra_line
                        result = send_message(text)
                        sys.stderr.write("[PUSH] send result: " + str(result) + "\n")
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



    @app.get("/api/v3/aliases/discover")
    async def api_v3_discover():
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
        
        # 加载已配置的别名
        alias_rows = conn.execute("SELECT alias_name FROM member_aliases").fetchall()
        configured_aliases = set()
        for (alias_name,) in alias_rows:
            configured_aliases.add(alias_name.strip().lower().replace(" ", ""))
        
        # 当前在线（来自 v3）
        from zoom_metrics import ZoomMetrics
        zm = ZoomMetrics()
        live_data = await zm.get_live()
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



    @app.get("/api/v3/sharing-live")
    async def api_v3_sharing_live():
        """共享状态：合并 LIVE_CACHE 在线集 + sharing_live 表 + webhook 事件"""
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
        merged = {}  # user_id -> sharing_info
        _online_set = set()  # normalize_identity_name of in_meeting participants
        sources = {"live_cache": 0, "sharing_live": 0, "webhook": 0}
        
        # Source 1: LIVE_CACHE（取代原 Metrics API 调用）
        # 从缓存获取在线列表，构建在线 identity 集合用于后续过滤
        live_data = LIVE_CACHE.get("data", {})
        online_list = live_data.get("online_list", []) or live_data.get("online", [])
        for p in online_list:
            name = p.get("name", "")
            if name:
                _online_set.add(db.normalize_identity_name(name))
        sources["live_cache"] = len(online_list)
        
        # Source 2: sharing_live table (is_active=1, not stale)
        live_rows = conn.execute(
            "SELECT * FROM sharing_live WHERE is_active=1 ORDER BY start_time DESC"
        ).fetchall()
        live_cols = [c[1] for c in conn.execute("PRAGMA table_info(sharing_live)").fetchall()]
        seen_names = set()  # deduplicate by user_name
        for r in live_rows:
            d = dict(zip(live_cols, r))
            user_name = d.get("user_name", "").strip()
            if not user_name:
                continue
            start_str = d.get("start_time", "")
            # Stale cutoff: >4h old
            if start_str:
                try:
                    sd = datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    if (now_utc - sd) > STALE_CUTOFF:
                        continue
                except: pass
            # Deduplicate: only keep the latest record per user_name
            normalized = db.normalize_identity_name(user_name)
            if normalized in seen_names:
                continue
            seen_names.add(normalized)
            uid = d.get("user_id", "")
            raw = user_name
            dn = db.resolve_display_name(raw)["display_name"]
            merged[uid] = {"name": dn, "raw_name": raw, "user_id": uid, "meeting_id": d.get("meeting_id", ""),
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
                uid = re.sub(r"[^0-9]", "", str(pt.get("user_id", "")))[:20]
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
            if uid not in merged:
                dn = db.resolve_display_name(info["raw_name"])["display_name"]
                merged[uid] = {"name": dn, "raw_name": info["raw_name"], "user_id": uid,
                               "meeting_id": info.get("meeting_id", ""),
                               "content": info.get("content", ""), "start_time": info.get("start_time", ""),
                               "source": "webhook_recovery"}
                sources["webhook_recovery"] = sources.get("webhook_recovery", 0) + 1
        # 过滤：只保留在线用户的共享记录。优先用 LIVE_CACHE，其次 Source 1 Metrics API
        _filter_set = next((lc.get("online_set", set()) for lc_name, lc in globals().items()
                           if lc_name == "LIVE_CACHE" and isinstance(lc, dict) and lc.get("data", {}).get("online_list")), None)
        if _filter_set is None:
            # LIVE_CACHE 不可用，用 Source 1 的 in_meeting 名单
            _filter_set = _online_set
        if _filter_set:
            for uid, info in list(merged.items()):
                if db.normalize_identity_name(info.get("name", "")) not in _filter_set:
                    del merged[uid]
        else:
            # Fallback: LIVE_CACHE/Source 1 均无在线数据，只保留最近 15 分钟内 active sharing
            _cutoff = (now_utc - timedelta(minutes=15)).isoformat()
            for uid, info in list(merged.items()):
                st = info.get("start_time", "")
                if st and st < _cutoff:
                    del merged[uid]
        
        # Build output
        active = []
        for uid, info in merged.items():
            st = info.get("start_time", "")
            m = max(0, mins_between(st))
            active.append({
                "name": info.get("name", ""),
                "raw_name": info.get("raw_name", ""),
                "user_id": uid,
                "meeting_id": info.get("meeting_id", ""),
                "content": info.get("content", ""),
                "start_time": st,
                "start_time_display": to_myt(st),
                "duration_minutes": m,
                "duration_display": disp(m),
                "source": info.get("source", ""),
            })
        
        return JSONResponse(
            headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0"},
            content={"ok": True, "current": len(active), "active": active, "sources": sources}
        )
        
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
    async def api_v3_member_discover():
        """自动发现历史 Zoom 用户名"""
        conn = db._get_conn()
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
            resolved = db.resolve_display_name(name)
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

    @app.get("/api/v3/member-display")
    async def api_v3_member_display_list():
        """所有显示名映射"""
        conn = db._get_conn()
        rows = conn.execute("SELECT * FROM member_display ORDER BY display_name").fetchall()
        cols = [c[1] for c in conn.execute("PRAGMA table_info(member_display)").fetchall()]
        return {"ok": True, "items": [dict(zip(cols, r)) for r in rows]}

    @app.post("/api/v3/member-display")
    async def api_v3_member_display_add(request: Request):
        data = await request.json()
        raw_name = data.get("raw_name", "").strip()
        display_name = data.get("display_name", "").strip()
        count_enabled = data.get("count_enabled", 1)
        note = data.get("note", "")
        if not raw_name or not display_name:
            return {"ok": False, "error": "raw_name 和 display_name 不能为空"}
        import re
        match_key = re.sub(r'\s+', '', raw_name.lower())
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        conn = db._get_conn()
        try:
            conn.execute(
                "INSERT OR REPLACE INTO member_display (raw_name, display_name, match_key, count_enabled, note, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (raw_name, display_name, match_key, int(count_enabled), note, now, now)
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
            "查看：https://zoom.dhbwang.xyz/sharing"
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
                                    resolved = db.resolve_display_name(name_raw)
                                    display_name = resolved["display_name"]
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
                                                + "查看：https://zoom.dhbwang.xyz/sharing"
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

    ZOOM_CLIENT_ID = "cLd9VdnGQxGETjM1u9pfg"
    ZOOM_CLIENT_SECRET = "1dvP5CbHHkpTG9XYxWXgkDMH3XB6kwiJ"
    REDIRECT_URI = "https://zoom.dhbwang.xyz/api/v2/auth/callback"

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
        params = f"response_type=code&client_id={ZOOM_CLIENT_ID}&redirect_uri={REDIRECT_URI}&state={state}"

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
            resolved = db.resolve_display_name(name)
            name = resolved["display_name"]
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

    # ── AI 分析 ────────────────────────────────────────────────────────────
    

    @app.get("/api/v3/live")
    async def api_v3_live():
        """实时在线数据（从 LIVE_CACHE 读取，不再主动调用 Zoom API）"""
        live_data = LIVE_CACHE.get("data", {})
        return {"ok": True, "data": live_data, "cache_ts": LIVE_CACHE["ts"]}


    @app.get("/api/v3/dashboard")
    async def api_v3_dashboard():
        """Dashboard 概览（读取 LIVE_CACHE，不再主动调用 Zoom API）"""
        conn = db._get_conn()
        report_start_utc, report_end_utc = myt_day_range_to_utc()

        # 从 LIVE_CACHE 读取在线状态
        cache_ts = LIVE_CACHE["ts"]
        cache_age = time.time() - cache_ts
        live_data = LIVE_CACHE.get("data", {})
        online_count = live_data.get("total_online", 0)
        meetings = live_data.get("meetings", [])
        sharing_list = LIVE_CACHE.get("sharing_list", [])
        sharing_count = len(sharing_list) if sharing_list else 0

        participant_count = conn.execute(
            "SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action_time < ?",
            (report_start_utc, report_end_utc)
        ).fetchone()[0]

        join_count = conn.execute(
            "SELECT COUNT(*) FROM zoom_participants WHERE action = 'enter' AND action_time >= ? AND action_time < ?",
            (report_start_utc, report_end_utc)
        ).fetchone()[0]

        leave_count = conn.execute(
            "SELECT COUNT(*) FROM zoom_participants WHERE action = 'leave' AND action_time >= ? AND action_time < ?",
            (report_start_utc, report_end_utc)
        ).fetchone()[0]

        # Dashboard participants = LIVE_CACHE online_list + DB 历史统计合并
        _ol = LIVE_CACHE.get("data", {}).get("online_list", [])
        _online_names = {db.normalize_identity_name(p.get("name","")) for p in _ol}
        # MYT 今日范围查 DB（历史统计）
        _myt_s, _myt_e = myt_day_range_to_utc()
        _hrows = conn.execute(
            "SELECT name, action, action_time FROM zoom_participants WHERE action_time >= ? AND action_time < ? ORDER BY action_time",
            (_myt_s, _myt_e)
        ).fetchall()
        # MYT 今日的历史聚合
        _hrows_list = [dict(zip([c[1] for c in conn.execute("PRAGMA table_info(zoom_participants)").fetchall()], r)) for r in _hrows]
        _hagg = build_participant_summary(_hrows_list)
        _hmap = {db.normalize_identity_name(p["name"]): p for p in _hagg}
        participants = []
        for _p in _ol:
            _nk = db.normalize_identity_name(_p.get("name",""))
            _hp = _hmap.get(_nk, {})
            participants.append({
                "name": _p.get("name", ""),
                "raw_name": _p.get("name", ""),
                "status": "\u5728\u7ebf\u4e2d",
                "session_duration": _p.get("duration_display", "\u2014"),
                "today_duration": _hp.get("total_duration") or _p.get("duration_display") or "\u2014",
                "leave_count": _hp.get("leave_count", 0),
                "last_active": _p.get("last_active", ""),
            })


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


    @app.get("/api/v2/ai-analysis")
    async def api_ai_analysis():
        """用 AI（DeepSeek）分析今日参会数据，生成自然语言报告"""
        # 先拿汇总数据
        summary_resp = await api_summary()
        if not summary_resp.get("ok"):
            return {"ok": False, "error": "无数据"}
        data = summary_resp

        # 构建 prompt
        stats = data.get("stats", {})
        members = data.get("members", [])
        meetings = data.get("meetings", [])

        prompt = f"""作为 Zoom 参会监控的 AI 分析助手，请分析今天的参会数据并生成中文报告。

今日概览：
- 总参与人数: {stats.get('total_participants', 0)} 人
- 参会记录数: {stats.get('total_records', 0)} 条
- 平均在线时长: {stats.get('avg_duration_min', 0)} 分钟
- 迟到人数: {stats.get('late_count', 0)} 人
- 早退人数: {stats.get('early_leave_count', 0)} 人

参会明细:
{chr(10).join(f'- {m["name"]}: {m["duration_min"]}分钟 (会议:{m["meeting_id"]})' + (' ⚠️迟到' if m.get('is_late') else '') + (' ⚠️早退' if m.get('is_early_leave') else '') for m in members[:30])}

会议室:
{chr(10).join(f'- 会议 {m["meeting_id"]}: {m["unique_people"]}人参与, 最早 {m["first_enter"]}, 最晚 {m["last_leave"]}' for m in meetings[:10])}

请生成一份简洁的总结报告（300字以内），包括：
1. 出勤概况
2. 异常标记（迟到/早退）
3. 建议"""

        try:
            import requests as req
            ai_resp = req.post("https://sub2api.dhbwang.xyz/v1/chat/completions",
                json={"model": "gpt-5.4-mini", "messages": [{"role": "user", "content": prompt}], "max_tokens": 500},
                headers={"Authorization": "Bearer " + (os.environ.get("SUB2API_KEY") or os.environ.get("DEEPSEEK_API_KEY", ""))},
                timeout=30)
            d = ai_resp.json()
            report = d["choices"][0]["message"]["content"] if d.get("choices") else "AI 分析暂不可用"
        except Exception as e:
            report = f"AI 分析暂时不可用（{str(e)}）"

        return {"ok": True, "report": report}

    @app.get("/api/v2/ai-daily-report")
    async def api_ai_daily_report():
        """Generate formatted AI daily report text for Telegram push"""
        import httpx
        from datetime import datetime, timezone, timedelta
        MYT = timezone(timedelta(hours=8))
        now_myt = datetime.now(MYT)
        date_str = now_myt.strftime("%Y-%m-%d")

        async with httpx.AsyncClient(timeout=10) as client:
            try:
                live_r = await client.get("http://localhost:8000/api/v2/live")
                live = live_r.json() if live_r.status_code == 200 else {}
            except:
                live = {}
            try:
                sum_r = await client.get("http://localhost:8000/api/v2/summary")
                summary = sum_r.json() if sum_r.status_code == 200 else {}
            except:
                summary = {}

        stats = summary.get("stats", {})
        members = summary.get("members", [])
        anomalies = live.get("anomalies", [])
        top_online = live.get("top_online", [])
        top_active = live.get("top_active", [])
        ai_summary = live.get("ai_summary", "")
        online_count = live.get("total_online", 0)
        total_participants = live.get("unique_participants", stats.get("total_participants", 0))
        total_events = live.get("total_events", 0)

        lines = []
        lines.append(f"\U0001f4ca Zoom Monitor \u65e5\u62a5")
        lines.append(f"\U0001f4c5 {date_str}")
        lines.append("")
        late = stats.get("late_count", 0)
        early = stats.get("early_leave_count", 0)
        risk = late + early
        lines.append(f"\U0001f465 \u53c2\u4e0e: {total_participants}  |  \U0001f7e2 \u5728\u7ebf: {online_count}")
        lines.append(f"\U0001f4cb \u4e8b\u4ef6: {total_events}  |  \u26a0 \u98ce\u9669: {risk}")
        lines.append(f"\u23f1 \u5e73\u5747: {stats.get('avg_duration_min', 0)} \u5206\u949f")
        lines.append("")

        if top_active:
            lines.append("\U0001f3c6 \u6700\u6d3b\u8dc3")
            for ta in top_active[:3]:
                lines.append(f"  {ta['name']} \u2014 {ta['count']} \u6b21")
            lines.append("")
        if top_online:
            lines.append("\u23f1 \u6700\u957f\u5728\u7ebf")
            for to in top_online[:3]:
                lines.append(f"  {to['name']} \u2014 {to['duration_display']}")
            lines.append("")
        if anomalies:
            lines.append("\U0001f6a8 \u98ce\u9669")
            for a in anomalies[:5]:
                lines.append(f"  \u26a0 {a['name']} \u2014 {', '.join(a['flags'])} ({a['actions']}\u6b21)")
            lines.append("")
        if ai_summary:
            lines.append("\U0001f916 AI \u5206\u6790")
            lines.append(f"  {ai_summary}")
            lines.append("")
        if late > 0 or early > 0:
            lines.append(f"\u26a0 \u5f02\u5e38: \u8fdf\u5230 {late}  |  \u65e9\u9000 {early}")
            risk_members = [m for m in members if m.get("is_late") or m.get("is_early_leave")]
            for rm in risk_members[:5]:
                tags = []
                if rm.get("is_late"): tags.append("\u8fdf\u5230")
                if rm.get("is_early_leave"): tags.append("\u65e9\u9000")
                lines.append(f"  {rm['name']} ({', '.join(tags)})")

        return {"ok": True, "report": "\n".join(lines), "date": date_str}

    @app.get("/api/v2/tg-send-daily-report")
    async def api_tg_send_daily_report():
        """Send AI daily report to Telegram"""
        import httpx
        report_resp = await api_ai_daily_report()
        if not report_resp.get("ok"):
            return {"ok": False, "error": "Daily report generation failed"}
        text = report_resp["report"]
        token = settings.telegram_bot_token
        chat_id = settings.telegram_group_chat_id or settings.telegram_private_chat_id
        if not token or not chat_id:
            return {"ok": False, "error": "Telegram not configured"}
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                r = await client.post(
                    f"https://api.telegram.org/bot{token}/sendMessage",
                    json={"chat_id": chat_id, "text": text},
                )
                return {"ok": r.json().get("ok", False)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

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
                            jt = p.get("join_time", "")
                            is_sh = p.get("share_application") or p.get("share_desktop") or p.get("share_whiteboard")
                            sc = "application" if p.get("share_application") else ("desktop" if p.get("share_desktop") else ("whiteboard" if p.get("share_whiteboard") else ""))
                            try:
                                r = db.resolve_display_name(name)
                                dn = r["display_name"]
                            except: dn = name
                            mins = 0; disp = ""; jtd = ""
                            if jt:
                                try:
                                    jd = datetime.fromisoformat(jt.replace("Z", "+00:00"))
                                    mins = max(0, int((now_utc - jd).total_seconds() / 60))
                                    disp = "{:d}h{:02d}".format(mins // 60, mins % 60) if mins >= 60 else "{}分钟".format(mins)
                                    jtd = jd.astimezone(MYT).strftime("%m-%d %H:%M:%S")
                                except: pass
                            # 会议还在进行中，参与者默认在线
                            ol = True
                            key = dn.lower().replace(" ", "")
                            top_active[dn] = top_active.get(dn, 0) + 1
                            if key not in participants_summary:
                                participants_summary[key] = {"name": dn, "is_online": ol,
                                    "last_active": jt, "last_active_display": jtd,
                                    "total_actions": 0, "duration_display": disp, "flags": [],
                                    "email": p.get("email", ""), "meeting_id": mid,
                                    "is_sharing": is_sh, "share_content": sc}
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
        ps_sorted = sorted(ps, key=lambda x: x.get("last_active", ""), reverse=True)
        max_online = max((m.get("raw_online_count", 0) for m in meetings.values()), default=0)
        ol_list = ps_sorted[:max(max_online, 1)] if max_online > 0 else []
        sl_list = [p for p in ol_list if p.get("is_sharing")]
        sa = sorted(top_active.items(), key=lambda x: -x[1])[:3]
        # 重建 meetings：保留所有原始会议信息，用 filtered online_count 替代 raw
        _m_map = {}
        for _mid, _bm in meetings.items():
            _m_map[_mid] = {"meeting_id": _mid, "topic": _bm.get("topic", _mid),
                            "online_count": 0, "raw_online_count": _bm.get("raw_online_count", 0),
                            "elapsed_minutes": _bm.get("elapsed_minutes", 0),
                            "start_time": _bm.get("start_time", "")}
        for _op in ol_list:
            _mid = _op.get("meeting_id", "")
            if _mid in _m_map:
                _m_map[_mid]["online_count"] += 1
        _raw_count = max((m.get("raw_online_count", 0) for m in meetings.values()), default=0)
        return {
            "ok": True,
            "total_online": len(ol_list),
            "meeting_active": bool(meetings) or _raw_count > 0,
            "raw_online_count": _raw_count,
            "sharing_list": sl_list,
            "meetings": list(_m_map.values()),
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
                            result = await _build_live_from_metrics(md, token)
                            LIVE_CACHE["ts"] = time.time()
                            LIVE_CACHE["data"] = result
                            LIVE_CACHE["sharing_list"] = result.get("sharing_list", [])
                            LIVE_CACHE["online_set"] = {db.normalize_identity_name(db.resolve_display_name(_op.get("name", ""))["display_name"]) for _op in result.get("online_list", [])}
                            return result
        except Exception as _live_err:
            import sys as _sys
            print("LIVE_API_ERR: %s" % _live_err, file=_sys.stderr)
            _cached = LIVE_CACHE.get("data", {})
            if _cached and _cached.get("total_online", -1) >= 0:
                _cached["cache_stale"] = True
                return _cached
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
        for (name,) in unique_names_rows:
            enters = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND name = ? AND action = 'enter'", (rs_utc, re_utc, name)).fetchone()[0]
            leaves = conn.execute("SELECT COUNT(*) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND name = ? AND action = 'leave'", (rs_utc, re_utc, name)).fetchone()[0]
            total_actions = enters + leaves
            last_time = conn.execute("SELECT MAX(action_time) FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND name = ?", (rs_utc, re_utc, name)).fetchone()[0]
            last_action = conn.execute("SELECT action FROM zoom_participants WHERE action_time >= ? AND action_time < ? AND name = ? ORDER BY action_time DESC LIMIT 1", (rs_utc, re_utc, name)).fetchone()
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
            """, (name, today)).fetchall()
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

        result = {"ok": True, "online": online, "meetings": meetings_online, "total_online": len(online),
                "total_events": total_events, "unique_participants": unique_participants,
                "unique_online_rate": unique_online_rate,
                "top_online": top_online, "active_hours": active_hours,
                "top_active": [{"name": r[0], "count": r[1]} for r in top_active],
                "participants_summary": participants_summary,
                "anomalies": anomalies, "health_level": health_level,
                "ai_summary": ai_summary}
        # 写入 LIVE_CACHE（SQL fallback 路径）
        LIVE_CACHE["ts"] = time.time()
        # 统一字段名：SQL 路径用 online，Metrics API 路径用 online_list
        cache_data = dict(result)
        cache_data["online_list"] = online
        LIVE_CACHE["data"] = cache_data
        LIVE_CACHE["online_set"] = {db.normalize_identity_name(db.resolve_display_name(_op.get("name", ""))["display_name"]) for _op in cache_data.get("online_list", [])}
        return result

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
