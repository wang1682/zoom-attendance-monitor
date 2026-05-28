"""main.py — FastAPI 应用入口

Phase 8: API Token 中间件（解析 X-API-Key / Authorization: Bearer）
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.settings import settings
from app.database import init_db, SyncSession
from app.models import Tenant
from phase2 import init_db as init_phase2_db


def _ensure_default_tenant_tokens():
    """确保所有已有 tenant 有 API Token"""
    import uuid
    from app.database import SyncSession
    from app.models import Tenant
    with SyncSession() as s:
        tenants = s.query(Tenant).all()
        for t in tenants:
            if not t.api_token:
                t.api_token = f"zm_{uuid.uuid4().hex}"
        s.commit()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # 启动时：建表 + 默认租户 + 初始化 API Token
    init_db()
    init_phase2_db()
    _ensure_default_tenant_tokens()
    yield


app = FastAPI(
    title="Zoom Attendance Intelligence Platform",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ===== API Token 中间件（对所有 API 请求） =====
@app.middleware("http")
async def api_token_middleware(request: Request, call_next):
    """解析 X-API-Key 或 Authorization: Bearer，注入 request.state.tenant_id"""
    path = request.url.path

    # 跳过非 API 路径
    skip_paths = ("/login", "/logout", "/static", "/dashboard", "/webhook")
    if any(path.startswith(p) for p in skip_paths):
        request.state.tenant_id = "default"
        request.state.auth_method = "session"
        response = await call_next(request)
        return response

    # Webhook 路径跳过 token 验证（靠签名验证）
    if path.startswith("/webhook"):
        request.state.tenant_id = settings.default_tenant_id
        request.state.auth_method = "webhook"
        response = await call_next(request)
        return response

    # 健康检查跳过
    if path.endswith("/health"):
        request.state.tenant_id = "default"
        request.state.auth_method = "none"
        response = await call_next(request)
        return response

    # 从 Header 提取 token
    token = request.headers.get("x-api-key", "") or request.headers.get("authorization", "")
    if token.startswith("Bearer "):
        token = token[7:].strip()

    if not token:
        # 向后兼容：无 token -> default tenant
        request.state.tenant_id = "default"
        request.state.auth_method = "none"
        response = await call_next(request)
        return response

    # 验证 token
    if token:
        try:
            with SyncSession() as s:
                tenant = s.query(Tenant).filter(
                    Tenant.api_token == token, Tenant.active == True
                ).first()

                if not tenant:
                    return JSONResponse(status_code=403, content={"detail": "Invalid or inactive API token"})

                request.state.tenant_id = tenant.id
                request.state.auth_method = "token"
                request.state.auth_tenant = tenant
        except Exception as e:
            return JSONResponse(status_code=500, content={"detail": f"Auth error: {str(e)}"})

    response = await call_next(request)
    return response


# 注册 API
from app.api.v1 import router as v1_router
app.include_router(v1_router)

from app.api.v1.webhook import router as webhook_router
app.include_router(webhook_router)

from phase2.api import router as v2_router
app.include_router(v2_router)

from phase2.dashboard import router as dashboard_router
app.include_router(dashboard_router)

from phase2.config_api import router as config_router
app.include_router(config_router)

from app.api.analytics import router as analytics_router
app.include_router(analytics_router)

# Phase 8: 多租户管理
from phase2.admin import router as admin_router
app.include_router(admin_router)

from fastapi.staticfiles import StaticFiles
import pathlib
static_dir = pathlib.Path(__file__).parent.parent / "static"
static_dir.mkdir(exist_ok=True)
app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


@app.get("/")
async def root():
    return {
        "service": "Zoom Attendance Intelligence Platform",
        "version": "0.1.0",
        "docs": "/docs",
    }
