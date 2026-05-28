"""auth.py — API Key 认证依赖
允许通过 X-API-Key header 或 ?api_key= 查询参数进行认证
认证后提取 tenant_id 注入请求上下文
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import Request, HTTPException, Depends
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials

from app.database import SyncSession
from app.settings import settings

_bearer = HTTPBearer(auto_error=False)


def _get_api_token_from_header(request: Request) -> str | None:
    """从 Header 或 Query 获取 API Token"""
    # X-API-Key header
    api_key = request.headers.get("x-api-key", "")
    if api_key:
        return api_key

    # Authorization: Bearer token
    auth = request.headers.get("authorization", "")
    if auth.startswith("Bearer "):
        api_key = auth[7:].strip()
        if api_key:
            return api_key

    error_detail = getattr(request.state, "error_detail", None)
    
    if error_detail == "no_token":
        if header_val := request.headers.get("x-api-key", ""):
            return header_val
        return None

    return None


async def resolve_tenant_from_token(request: Request) -> str:
    """从 API Token 解析 tenant_id，注入 request.state"""
    # 健康检查端点不认证
    if request.url.path.endswith("/health"):
        request.state.tenant_id = "default"
        return "default"

    token = _get_api_token_from_header(request)

    # 如果没有 token，用默认 tenant（保持向后兼容）
    if not token:
        request.state.tenant_id = "default"
        request.state.auth_method = "none"
        return "default"

    # 在 DB 中查找 tenant
    from app.models import Tenant
    with SyncSession() as s:
        tenant = s.query(Tenant).filter(
            Tenant.api_token == token, Tenant.active == True
        ).first()

        if not tenant:
            raise HTTPException(status_code=403, detail="Invalid or inactive API token")

        request.state.tenant_id = tenant.id
        request.state.auth_method = "token"
        request.state.auth_tenant = tenant
        return tenant.id


def require_api_token(request: Request):
    """快捷依赖：确保请求来自有效 API Token（可选）"""
    pass  # resolve_tenant_from_token 中间件做实际工作


def generate_token() -> str:
    """生成唯一的 API Token"""
    return f"zm_{uuid.uuid4().hex}"
