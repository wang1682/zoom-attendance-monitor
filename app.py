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
from datetime import datetime, timezone
from pathlib import Path

from config import settings
import db

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
    from fastapi.responses import HTMLResponse
    from fastapi.staticfiles import StaticFiles
    from fastapi.templating import Jinja2Templates

    app = FastAPI(title=BRAND["app_name_zh"], version="2.0.0")
    app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")
    tmpl = Jinja2Templates(directory=str(BASE_DIR / "templates"))

    # ── DB 初始化中间件 ─────────────────────────────────────────────────────
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
    @app.get("/", response_class=HTMLResponse)
    async def landing(request: Request):
        """Landing Page — 销售介绍页"""
        # 尝试加载 demo 数据用于预览
        try:
            demo_data = _ensure_demo().get_demo_stats()
            demo_participants = _ensure_demo().get_demo_participants(limit=5)
        except Exception:
            demo_data = {"participant_count": 12, "new_face_count": 3, "checkin_rate": 78, "alert_count": 2}
            demo_participants = [
                {"name": "张三", "email": "zhang@example.com", "action_time": "08:32", "is_new": False},
                {"name": "李四", "email": "li@example.com", "action_time": "08:45", "is_new": False},
                {"name": "新用户", "email": "new@example.com", "action_time": "09:02", "is_new": True},
            ]
        return tmpl.TemplateResponse(request, "landing.html", {
            "brand": BRAND,
            "demo_data": demo_data,
            "demo_participants": demo_participants,
        })

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_page(request: Request):
        if settings.demo_mode:
            demo = _ensure_demo()
            demo.seed_demo_data()
            participants = demo.get_demo_participants()
            alerts = demo.get_demo_alerts()
            stats = demo.get_demo_stats()
        else:
            participants = db.get_today_participants(limit=100)
            alerts = db.get_recent_alerts(limit=20)
            stats = {
                "participant_count": len(participants),
                "alert_count": len(alerts),
                "new_face_count": 0,
                "checkin_rate": 0,
            }
        return tmpl.TemplateResponse(request, "dashboard.html", {
            "today": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "participants": participants,
            "alerts": alerts,
            "stats": stats,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
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
    async def participants_page(request: Request):
        if settings.demo_mode:
            participants = _ensure_demo().get_demo_participants()
        else:
            participants = db.get_today_participants(limit=200)
        return tmpl.TemplateResponse(request, "participants.html", {
            "participants": participants,
            "brand": BRAND,
            "demo_mode": settings.demo_mode,
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
            reports = []
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

    # ── API ──────────────────────────────────────────────────────────────────
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
    @app.post("/webhook")
    async def zoom_webhook(request: Request):
        if settings.demo_mode:
            return {"ok": True, "demo": True}

        body = await request.body()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            raise HTTPException(400, "invalid JSON")

        signature = request.headers.get("x-zm-signature", "")
        if settings.zoom_webhook_secret and signature:
            expected = hmac.new(
                settings.zoom_webhook_secret.encode(),
                body,
                hashlib.sha256,
            ).hexdigest()
            if not hmac.compare_digest(expected, signature):
                sys.stderr.write("[WEBHOOK] 签名验证失败\n")
                raise HTTPException(403, "signature mismatch")

        event_type = payload.get("event", "")
        db.save_webhook_event(event_type, payload)
        sys.stdout.write(f"[WEBHOOK] {event_type}\n")
        sys.stdout.flush()

        if event_type in ("meeting.participant_joined", "meeting.participant_left"):
            obj = payload.get("payload", {}).get("object", payload.get("object", {}))
            participant = obj.get("participant", {})
            meeting_id = str(obj.get("id", ""))
            name = participant.get("user_name", "").strip()
            email = participant.get("email", "")
            action = "enter" if event_type == "meeting.participant_joined" else "leave"
            action_time = datetime.now(timezone.utc)
            if name and action:
                db.save_participant(meeting_id, name, email, action, action_time,
                                    source="webhook")

        return {"ok": True}

    # ── 健康检查 ─────────────────────────────────────────────────────────────
    @app.get("/health")
    async def health():
        return {
            "status": "ok",
            "demo_mode": settings.demo_mode,
            "time": datetime.now(timezone.utc).isoformat(),
        }

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
