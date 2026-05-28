"""
app.py — Zoom 参会监控统一入口
Modes:
  python app.py api       → FastAPI dashboard on port 8000
  python app.py webhook   → FastAPI webhook receiver on port 9000
  python app.py monitor   → Polling service (no FastAPI deps needed)
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
            from demo_data import get_demo_stats
            demo_data = get_demo_stats()
        except Exception:
            demo_data = {
                "participant_count": 12,
                "new_face_count": 3,
                "checkin_rate": 78,
                "alert_count": 2,
                "recent_participants": [
                    {"name": "张三", "email": "zhang@example.com", "time": "08:32", "is_new": False},
                    {"name": "李四", "email": "li@example.com", "time": "08:45", "is_new": False},
                    {"name": "新用户", "email": "new@example.com", "time": "09:02", "is_new": True},
                ],
            }
        return tmpl.TemplateResponse(request, "landing.html", {
            "brand": BRAND,
            "demo_data": demo_data,
        })

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard(request: Request):
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        participants = db.get_today_participants(limit=100)
        alerts = db.get_recent_alerts(limit=20)
        return tmpl.TemplateResponse(request, "dashboard.html", {
            "today": today,
            "participants": participants,
            "alerts": alerts,
            "participant_count": len(participants),
            "alert_count": len(alerts),
            "brand": BRAND,
        })

    # ── Demo ──────────────────────────────────────────────────────────────────
    @app.get("/demo", response_class=HTMLResponse)
    async def demo_page(request: Request, tab: str = "overview"):
        """Demo 模式 — 免 Zoom 账号体验"""
        from demo_data import seed_demo_data, get_demo_stats, get_demo_alerts, get_demo_participants
        seed_demo_data()
        stats = get_demo_stats()
        alerts = get_demo_alerts()
        participants = get_demo_participants()
        return tmpl.TemplateResponse(request, "demo.html", {
            "brand": BRAND,
            "stats": stats,
            "alerts": alerts,
            "participants": participants,
            "active_tab": tab,
        })

    @app.get("/api/demo/reset")
    async def demo_reset():
        """重置 demo 数据并重新 seed"""
        from demo_data import reset_demo, seed_demo_data, get_demo_stats
        reset_demo()
        seed_demo_data()
        return get_demo_stats()

    @app.get("/events", response_class=HTMLResponse)
    async def events_page(request: Request):
        events = db.get_recent_events(limit=100)
        return tmpl.TemplateResponse(request, "events.html", {
            "events": events,
            "brand": BRAND,
        })

    @app.get("/participants", response_class=HTMLResponse)
    async def participants_page(request: Request):
        participants = db.get_today_participants(limit=200)
        return tmpl.TemplateResponse(request, "participants.html", {
            "participants": participants,
            "brand": BRAND,
        })

    @app.get("/alerts", response_class=HTMLResponse)
    async def alerts_page(request: Request):
        alerts = db.get_recent_alerts(limit=100)
        return tmpl.TemplateResponse(request, "alerts.html", {
            "alerts": alerts,
            "brand": BRAND,
        })

    # ── API ──────────────────────────────────────────────────────────────────
    @app.get("/api/participants")
    async def api_participants(limit: int = 200):
        return db.get_today_participants(limit=limit)

    @app.get("/api/alerts")
    async def api_alerts(limit: int = 50):
        return db.get_recent_alerts(limit=limit)

    @app.get("/api/events")
    async def api_events(limit: int = 50):
        return db.get_recent_events(limit=limit)

    # ── Webhook ──────────────────────────────────────────────────────────────
    @app.post("/webhook")
    async def zoom_webhook(request: Request):
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
                sys.stderr.write(f"[WEBHOOK] 签名验证失败\n")
                raise HTTPException(403, "signature mismatch")

        event_type = payload.get("event", "")
        db.save_webhook_event(event_type, payload)
        sys.stdout.write(f"[WEBHOOK] {event_type}\n")
        sys.stdout.flush()

        if event_type in ("meeting.participant_joined", "meeting.participant_left"):
            obj = payload.get("object", {})
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
        return {"status": "ok", "time": datetime.now(timezone.utc).isoformat()}

    _app = app
    return app


# ── 启动入口 ──────────────────────────────────────────────────────────────────

def start_api():
    import uvicorn
    settings.validate_required()
    app = build_app()
    uvicorn.run(app, host=settings.api_host, port=settings.api_port, log_level="info")


def start_webhook():
    import uvicorn
    settings.validate_required()
    app = build_app()
    uvicorn.run(app, host=settings.webhook_host, port=settings.webhook_port, log_level="info")


def start_monitor():
    settings.validate_required()
    db.init_db()
    from monitor import monitor_loop
    asyncio.run(monitor_loop())


def start_command():
    """启动 Telegram 指令轮询（独立服务，不阻塞）"""
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
    else:
        print(f"Usage: python app.py [api|webhook|monitor|command]")
        _sys.exit(1)
