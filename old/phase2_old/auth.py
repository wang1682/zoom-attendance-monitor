"""
phase2/auth.py — 完整认证系统

- /login POST（bcrypt 验证 + session cookie）
- /logout（清除 cookie）
- require_auth() 中间件（dashboard 页面的 cookie 校验）
- require_api_auth() 中间件（API 路由的 cookie 或 Bearer Token 校验）
- require_role() / require_any_role() 角色权限依赖
- 所有安全事件写入 audit_logs 表
- 支持多租户：session 中存储 tenant_id
"""
from __future__ import annotations

from datetime import timedelta, datetime, timezone
from typing import Optional, Sequence, Union
from uuid import uuid4

from fastapi import Request, Response, HTTPException
from itsdangerous import URLSafeTimedSerializer, BadSignature, SignatureExpired

from app.settings import settings
from phase2.audit import log_audit
from phase2.db import SyncSession
from phase2.models import DashboardUser, Tenant, ROLE_HIERARCHY, ROLE_SCOPES


# 序列化器 — 用 settings.secret_key 签名
_serializer = URLSafeTimedSerializer(settings.secret_key, salt="dashboard-auth")

COOKIE_NAME = "zoom_session"
COOKIE_MAX_AGE = 3600 * 8  # 8 小时
COOKIE_PATH = "/"
COOKIE_SECURE = False  # HTTPS 反代时设为 True
COOKIE_HTTPONLY = True
COOKIE_SAMESITE = "lax"


# ─── 多租户辅助 ──────────────────────

def _login_by_db_user(username: str, password: str) -> tuple[Optional[DashboardUser], str]:
    """从 dashboard_users 表验证登录"""
    import bcrypt
    with SyncSession() as s:
        user = s.query(DashboardUser).filter_by(username=username, active=True).first()
        if not user:
            return None, "用户不存在"
        try:
            if bcrypt.checkpw(password.encode(), user.password_hash.encode()):
                return user, "ok"
        except Exception:
            pass
        return None, "密码错误"


def _login_by_env(username: str, password: str) -> tuple[Optional[str], str]:
    """从 .env 设置的 admin 验证（向后兼容）"""
    if settings.dashboard_admin_user and settings.dashboard_admin_password_hash:
        import bcrypt
        try:
            if (username == settings.dashboard_admin_user
                and bcrypt.checkpw(password.encode(), settings.dashboard_admin_password_hash.encode())):
                return "default", "ok"  # .env admin 默认 default 租户
        except Exception:
            pass
    return None, "密码错误"


def verify_password(plain_password: str) -> bool:
    """验证明文密码是否匹配 .env 中的 hash"""
    import bcrypt
    if not settings.dashboard_admin_password_hash:
        return False
    try:
        return bcrypt.checkpw(
            plain_password.encode("utf-8"),
            settings.dashboard_admin_password_hash.encode("utf-8"),
        )
    except (ValueError, AttributeError, TypeError):
        return False


def create_session(username: str, tenant_id: str = "default") -> str:
    """创建签名的 session cookie 值"""
    payload = {
        "user": username,
        "uid": uuid4().hex[:8],
        "tenant_id": tenant_id,
        "iat": datetime.now(timezone.utc).isoformat(),
    }
    return _serializer.dumps(payload)


def read_session(cookie_value: str) -> Optional[dict]:
    """验证 session cookie 并返回 payload dict，失败返回 None"""
    try:
        data = _serializer.loads(cookie_value, max_age=COOKIE_MAX_AGE)
        return data
    except (BadSignature, SignatureExpired, TypeError):
        return None


def set_session_cookie(response: Response, username: str, tenant_id: str = "default"):
    """设置登录 cookie"""
    token = create_session(username, tenant_id=tenant_id)
    response.set_cookie(
        key=COOKIE_NAME,
        value=token,
        max_age=COOKIE_MAX_AGE,
        path=COOKIE_PATH,
        secure=COOKIE_SECURE,
        httponly=COOKIE_HTTPONLY,
        samesite=COOKIE_SAMESITE,
    )


def clear_session_cookie(response: Response):
    """清除登录 cookie（登出）"""
    response.set_cookie(
        key=COOKIE_NAME,
        value="",
        max_age=0,
        path=COOKIE_PATH,
        secure=COOKIE_SECURE,
        httponly=COOKIE_HTTPONLY,
        samesite=COOKIE_SAMESITE,
    )


def get_current_user(request: Request) -> Optional[dict]:
    """从请求中读取当前登录用户 session，未登录返回 None"""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    return read_session(cookie)


# ─── 角色权限系统 ────────────────────────────────────────

def _get_user_role(username: str) -> Optional[str]:
    """查询用户在当前 DB 中的 role"""
    if not username or username in ("anonymous", "api_token"):
        return None
    with SyncSession() as s:
        user = s.query(DashboardUser).filter_by(username=username, active=True).first()
        if user:
            return user.role
    return None


def check_role(session_user: Optional[dict], min_role: str) -> Optional[str]:
    """检查用户是否有 >= min_role 的权限。返回 None=通过，str=错误原因。"""
    if not session_user:
        return "未登录"
    username = session_user.get("user", "")
    role = _get_user_role(username)
    if not role:
        # .env admin 向后兼容：视为 owner
        if username == settings.dashboard_admin_user:
            role = "owner"
        else:
            return "无角色权限"
    if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY.get(min_role, 0):
        return f"权限不足：需要 {min_role} 及以上角色，当前为 {role}"
    return None


def require_role(min_role: str):
    """FastAPI Depends 工厂：检查用户角色 >= min_role

    用法：
        @router.get("/admin/users")
        async def list_users(user: dict = Depends(require_role("owner"))):
            ...

    返回 dict 包含 user/tenant_id/role，权限不足时抛 403 HTTPException。
    """
    async def _check(request: Request) -> dict:
        session_user = get_current_user(request)
        if not session_user:
            raise HTTPException(status_code=401, detail="未登录")
        username = session_user.get("user", "")
        tenant_id = session_user.get("tenant_id", "default")

        # 取 role
        role = _get_user_role(username)
        if not role and username == settings.dashboard_admin_user:
            role = "owner"

        if not role:
            raise HTTPException(status_code=403, detail=f"用户 {username} 无角色权限")

        if ROLE_HIERARCHY.get(role, 0) < ROLE_HIERARCHY.get(min_role, 0):
            raise HTTPException(
                status_code=403,
                detail=f"权限不足：需要 {min_role} 及以上角色，当前为 {role}",
            )

        session_user["role"] = role
        return session_user

    return _check


def require_any_role(*allowed_roles: str):
    """FastAPI Depends 工厂：检查用户角色是否在 allowed_roles 中

    用法：
        @router.get("/config")
        async def config(user: dict = Depends(require_any_role("admin", "owner"))):
            ...
    """
    async def _check(request: Request) -> dict:
        session_user = get_current_user(request)
        if not session_user:
            raise HTTPException(status_code=401, detail="未登录")
        username = session_user.get("user", "")
        tenant_id = session_user.get("tenant_id", "default")

        role = _get_user_role(username)
        if not role and username == settings.dashboard_admin_user:
            role = "owner"

        if not role or role not in allowed_roles:
            raise HTTPException(
                status_code=403,
                detail=f"权限不足：需要以下角色之一 {', '.join(allowed_roles)}，当前为 {role or '无'}",
            )

        session_user["role"] = role
        return session_user

    return _check


def check_owner_last(s: SyncSession, tenant_id: str, user_id: str = None) -> tuple[bool, str]:
    """检查 tenant 是否至少还有一个 owner（用于防止删除/降级最后一个 owner）

    返回 (is_last_owner, message)
    - 如果当前用户是最后一个 owner，操作将被阻止
    """
    owners = s.query(DashboardUser).filter_by(
        tenant_id=tenant_id, role="owner", active=True
    ).count()
    if owners <= 1 and user_id:
        current = s.query(DashboardUser).filter_by(id=user_id).first()
        if current and current.role == "owner":
            return True, "不能移除最后一个 owner"
    return False, ""


# ─── 中间件函数 ──────────────────────────────────────────

def require_auth(request: Request) -> Optional[dict]:
    """Dashboard 页面鉴权：仅检查 cookie。返回 session dict 或 None"""
    return get_current_user(request)


API_SKIP_PATHS = {"/api/v2/health", "/api/v2/"}
_API_TOKEN_CACHE: dict[str, float] = {}  # token → expires_at


def _validate_bearer_token(token: str) -> bool:
    """验证 Bearer Token

    支持从 .env API_TOKEN 读取的静态 token，或 session cookie 兼容。
    """
    # 静态 API Token 模式
    if settings.api_token and token == settings.api_token:
        return True

    # Session cookie 兼容（允许 curl 用 cookie value 做 Bearer）
    try:
        user = read_session(token)
        return user is not None
    except Exception:
        return False


def require_api_auth(request: Request) -> dict:
    """API 路由鉴权（用于中间件中检查）

    1. 优先检查 session cookie（浏览器访问）
    2. 其次检查 Authorization: Bearer ***

    返回 session dict，未认证时抛出 401
    """
    path = request.url.path.rstrip("/")

    # 公开路径跳过
    if path in API_SKIP_PATHS:
        return {"user": "anonymous", "tenant_id": "default"}

    # 开头的 /api/v2/ 才检查
    if not path.startswith("/api/v2"):
        return {"user": "anonymous", "tenant_id": "default"}

    # 1. cookie 登录态
    user = get_current_user(request)
    if user:
        return user

    # 2. Bearer Token
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if _validate_bearer_token(token):
            return {"user": "api_token", "tenant_id": "default"}
    elif auth_header.startswith("Token "):
        token = auth_header[6:]
        if _validate_bearer_token(token):
            return {"user": "api_token", "tenant_id": "default"}

    # 未认证
    raise HTTPException(
        status_code=401,
        detail="Unauthorized",
        headers={"WWW-Authenticate": "Bearer"},
    )


# ─── 登录/登出处理 ──────────────────────────────────────

def handle_login(request: Request, response: Response, password: str) -> tuple[bool, str]:
    """处理登录请求

    流程：
    1. 优先查 dashboard_users 表（多租户）
    2. 回退 .env admin（向后兼容）
    3. 审计日志

    返回 (success: bool, message: str)
    """
    username = settings.dashboard_admin_user or "admin"

    # 1. 先查 DB 用户（多租户）
    db_user, msg = _login_by_db_user(username, password)
    if db_user:
        set_session_cookie(response, db_user.username, tenant_id=db_user.tenant_id)
        log_audit("login_success", username=db_user.username, request=request,
                  tenant_id=db_user.tenant_id)
        return True, db_user.username

    # 2. 回退 .env admin
    env_tenant, msg = _login_by_env(username, password)
    if env_tenant:
        set_session_cookie(response, username, tenant_id=env_tenant)
        log_audit("login_success", username=username, request=request,
                  tenant_id=env_tenant)
        return True, username

    # 3. 登录失败
    log_audit("login_failed", username=username, request=request, detail=msg)
    return False, msg


def handle_logout(request: Request, response: Response):
    """处理登出"""
    user = get_current_user(request)
    if user:
        log_audit("logout", username=user.get("user", ""), request=request)
    clear_session_cookie(response)
