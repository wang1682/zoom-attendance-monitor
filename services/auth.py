"""auth.py — AuthService: RBAC 统一入口.

取代散落在各路由中的权限判断（require_role, require_user, get_current_user 等）。
所有角色/租户逻辑集中在这里，路由层只看返回结果。
"""

from __future__ import annotations

from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

from services.base import BaseService, ROLE_HIERARCHY, role_ge


# ── Exceptions ────────────────────────────────────────────────────────────────

class PermissionDenied(Exception):
    """Raised when user lacks the minimum role level."""
    def __init__(self, detail: str = "权限不足"):
        self.detail = detail

class NotAuthenticated(Exception):
    """Raised when no user session exists."""
    def __init__(self, detail: str = "未登录"):
        self.detail = detail


# ── Public helpers (for code that doesn't go through AuthService yet) ──

__all__ = [
    "AuthService",
    "PermissionDenied",
    "NotAuthenticated",
    "ROLE_HIERARCHY",
    "role_ge",
]


# ── AuthService ───────────────────────────────────────────────────────────────

class AuthService(BaseService):
    """RBAC 统一入口。

    所有路由层权限判断统一调用 AuthService 的方法。

    用法::

        auth = AuthService(request)
        ctx = auth.require("viewer")        # 确保已登录，返回 AuthContext
        ctx = auth.require("admin")         # 需要 admin 以上角色
        auth.can_manage(target_user)        # 检查能否管理目标用户

    ```"""

    # ── Authentication ─────────────────────────────────────────────────

    def require(self, min_role: str = "viewer") -> "AuthContext":
        """Ensure authenticated + role >= min_role.

        Returns AuthContext on success.
        Raises NotAuthenticated or PermissionDenied on failure.
        """
        ctx = self._require_context()

        if not ctx.user_id:
            raise NotAuthenticated()

        if ctx.role_level < ROLE_HIERARCHY.get(min_role, 0):
            raise PermissionDenied(f"需要 {min_role} 以上权限")

        return ctx

    def require_authenticated(self) -> "AuthContext":
        """Just check logged in, any role."""
        return self.require("viewer")

    def require_editor(self) -> "AuthContext":
        """viewer 不能修改."""
        return self.require("user")

    def require_admin(self) -> "AuthContext":
        """admin 或 super_admin."""
        return self.require("admin")

    def require_super_admin(self) -> "AuthContext":
        """仅 super_admin."""
        return self.require("super_admin")

    # ── Authorization ──────────────────────────────────────────────────

    @staticmethod
    def _check_role_can_manage(actor_role: str, target: dict) -> bool:
        """Pure-logic version: no request needed. Used by compatibility wrappers."""
        if actor_role == "super_admin":
            return True
        if actor_role == "admin":
            return target.get("role") != "super_admin"
        return False

    @staticmethod
    def _check_allowed_create_roles(actor_role: str) -> list[dict]:
        """Pure-logic version: no request needed. Used by compatibility wrappers."""
        if actor_role == "super_admin":
            return [
                {"value": "admin", "label": "管理员"},
                {"value": "user", "label": "租户管理员(user)"},
            ]
        if actor_role == "admin":
            return [
                {"value": "user", "label": "租户管理员(user)"},
            ]
        if actor_role == "user":
            return [
                {"value": "user", "label": "用户"},
            ]
        return []

    def can_manage(self, target: dict) -> bool:
        """Check if current user can manage (edit/delete/toggle) a target user.

        Only system admins can manage users:
          - super_admin: can manage everyone including self
          - admin: can manage everyone except super_admin
          - user / viewer / tenant: cannot manage anyone (use TenantMemberService in future)
        """
        ctx = self._require_context()

        if ctx.role == "super_admin":
            return True
        if ctx.role == "admin":
            return target.get("role") != "super_admin"
        return False

    def allowed_create_roles(self) -> list[dict]:
        """Roles the current user is allowed to create.

        Mirrors ``_allowed_create_roles`` from admin_routes.py.
        """
        ctx = self._require_context()

        if ctx.role == "super_admin":
            return [
                {"value": "admin", "label": "管理员"},
                {"value": "user", "label": "租户管理员(user)"},
            ]
        if ctx.role == "admin":
            return [
                {"value": "user", "label": "租户管理员(user)"},
            ]
        if ctx.role == "user":
            return []  # user is not a system manager
        return []

    # ── Tenant helpers ─────────────────────────────────────────────────

    def available_tenants(self) -> list[dict[str, Any]]:
        """Return tenants visible to the current user.

        - super_admin: all tenants
        - admin: linked tenants (via user_tenants table)
        - user / viewer: own tenant only
        """
        import db  # lazy import to avoid circular dep

        ctx = self._require_context()

        if ctx.is_super_admin():
            return db.get_all_tenants()

        if ctx.role == "admin":
            return db.get_user_tenants(ctx.user_id)

        # user / viewer
        t = db.get_tenant(ctx.tenant_id)
        if t:
            return [t]
        return []

    def get_effective_tenant_id(self) -> str:
        """Unified tenant ID resolution (replaces ``app.state.get_effective_tenant_id``).

        super_admin -> selected_tenant (switchable, falls back to "default")
        others      -> own tenant_id (bound)
        """
        return self._require_context().effective_tenant

    def is_viewer(self) -> bool:
        """Quick viewer check for templates."""
        ctx = self._require_context()
        return ctx.role == "viewer"

    def is_super_admin(self) -> bool:
        """Quick check for templates."""
        return self._require_context().is_super_admin()

    def is_admin_or_above(self) -> bool:
        return self._require_context().is_admin_or_above()

    # ── Nav helpers ────────────────────────────────────────────────────

    @staticmethod
    def _check_get_nav_items(role: str) -> list[dict]:
        """Pure-logic nav builder for compatibility wrappers (no request needed).
        
        - super_admin/admin: all items + 管理中心 (system-level management)
        - user: all tenant items EXCEPT 管理中心
        - viewer/tenant: tenant items only
        """
        items = [
            {"key": "overview",     "label": "总览",   "href": "/dashboard/",              "icon": ""},
            {"key": "participants", "label": "成员",   "href": "/dashboard/participants",   "icon": ""},
            {"key": "meetings",     "label": "会议",   "href": "/dashboard/meetings",       "icon": ""},
        ]
        if role in ("super_admin", "admin"):
            items.append({"key": "admin_center", "label": "管理中心", "href": "/dashboard/admin-center", "icon": ""})
        else:
            items.extend([
                {"key": "channels", "label": "推送频道", "href": "/dashboard/tenant/channels", "icon": ""},
                {"key": "security", "label": "安全中心", "href": "/dashboard/tenant/security", "icon": ""},
                {"key": "alerts",   "label": "预警",     "href": "/dashboard/alerts",            "icon": ""},
            ])
        return items

    def get_nav_items(self) -> list[dict]:
        """Build top nav filtered by role (replaces _get_nav_items).

        Mirrors the existing logic from tenant_routes.py _get_nav_items().
        """
        ctx = self._require_context()

        items = [
            {"key": "overview",     "label": "总览",   "href": "/dashboard/",              "icon": ""},
            {"key": "participants", "label": "成员",   "href": "/dashboard/participants",   "icon": ""},
            {"key": "meetings",     "label": "会议",   "href": "/dashboard/meetings",       "icon": ""},
        ]

        if ctx.is_admin_or_above():  # system-level management
            items.append({"key": "admin_center", "label": "管理中心", "href": "/dashboard/admin-center", "icon": ""})
        else:
            items.extend([
                {"key": "channels", "label": "推送频道", "href": "/dashboard/tenant/channels", "icon": ""},
                {"key": "security", "label": "安全中心", "href": "/dashboard/tenant/security", "icon": ""},
                {"key": "alerts",   "label": "预警",     "href": "/dashboard/alerts",         "icon": ""},
            ])

        return items

    def get_template_vars(self, active: str, **extra) -> dict:
        """Build the standard template context dict (replaces _render_tenant logic).

        Returns a dict suitable for Jinja2 TemplateResponse.
        """
        import db

        ctx = self._require_context()
        tenant_id = ctx.effective_tenant

        # Fresh user from DB
        fresh_user = db.get_user_by_id(ctx.user_id) or {}
        tenant_info = db.get_tenant(tenant_id)
        tenant_name = tenant_info.get("display_name", tenant_id) if tenant_info else tenant_id

        current_user = {
            "id": fresh_user.get("id", ctx.user_id),
            "username": fresh_user.get("username", ctx.username),
            "display_name": fresh_user.get("display_name", ""),
            "role": ctx.role,
            "tenant_id": tenant_id,
            "is_active": "true" if fresh_user.get("is_active") else "false",
            "telegram_chat_id": fresh_user.get("telegram_chat_id", ""),
            "telegram_2fa_enabled": fresh_user.get("telegram_2fa_enabled", 0),
            "telegram_2fa_verified_at": fresh_user.get("telegram_2fa_verified_at", ""),
            "twofa_backup_codes": fresh_user.get("twofa_backup_codes", ""),
        }

        all_tenants = self.available_tenants() if ctx.is_user_or_above() else []
        current_tenant_name = ""
        for t in all_tenants:
            if t.get("id") == tenant_id or t.get("tenant_id") == tenant_id:
                current_tenant_name = t.get("display_name", t.get("name", tenant_id))
                break
        if not current_tenant_name:
            current_tenant_name = tenant_name

        return {
            **extra,
            "active": active,
            "tenant_name": tenant_name,
            "is_viewer": ctx.role == "viewer",
            "is_super_admin": ctx.is_super_admin(),
            "available_tenants": all_tenants,
            "current_tenant_id": tenant_id,
            "current_tenant_name": current_tenant_name,
            "hide_settings": not ctx.is_user_or_above(),
            "current_user": current_user,
            "nav_items": self.get_nav_items(),
        }
