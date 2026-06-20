"""base.py — BaseService & AuthContext.

Every Service inherits BaseService and automatically extracts
current_user / tenant_id / role from the FastAPI Request.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request


# ── Role Hierarchy ────────────────────────────────────────────────────────────
# Mirrors db.ROLE_HIERARCHY but kept here so Service layer has zero DB dep
# for pure-auth operations.

ROLE_HIERARCHY: FinalRoleMap = {
    "super_admin": 4,
    "admin": 3,
    "user": 2,     # tenant-level admin — DB stores "user" for this role
    "viewer": 1,
    "tenant": 0,   # fallback — old session code returns "tenant" when no role is set
}


def role_ge(user_role: str, required_role: str) -> bool:
    """True if user_role level >= required_role level."""
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(required_role, 0)


# ── Auth Context ──────────────────────────────────────────────────────────────

@dataclass
class AuthContext:
    """Standardised auth info extracted from every incoming request.

    Populated by BaseService.__init__().  All Service subclasses work with
    this shape — never with raw ``request.session.get("role")``.
    """

    user_id: int
    username: str
    role: str
    role_level: int
    tenant_id: str
    selected_tenant: str | None = None
    pending_tenants: list[dict[str, Any]] = field(default_factory=list)

    # ── Convenience helpers ──────────────────────────────────────────────

    def is_super_admin(self) -> bool:
        return self.role == "super_admin"

    def is_admin_or_above(self) -> bool:
        return self.role in ("super_admin", "admin")

    def can_edit(self) -> bool:
        """viewer cannot edit; everyone else can."""
        return self.role != "viewer"

    def is_user_or_above(self) -> bool:
        """True for any user-level role or higher (user, admin, super_admin).
        This is the threshold for seeing tenant-level functionality."""
        return self.role in ("super_admin", "admin", "user")

    @property
    def effective_tenant(self) -> str:
        """super_admin uses selected_tenant (switchable), others use own tenant_id."""
        if self.is_super_admin():
            return self.selected_tenant or self.tenant_id or "default"
        return self.tenant_id or "default"


# ── Base Service ──────────────────────────────────────────────────────────────

class BaseService:
    """All service classes inherit from this.

    Usage::

        auth = AuthService(request)       # from services.auth import AuthService
        zoom = ZoomService(request)       # from services.zoom import ZoomService

    When instantiated without a ``request`` (background tasks, CLI scripts),
    subclasses must handle the missing context themselves.
    """

    def __init__(self, request: Request | None = None):
        self.request = request
        self.context: AuthContext | None = None
        if request is not None:
            self.context = self._extract_context(request)

    @staticmethod
    def _extract_context(request: Request) -> AuthContext:
        """Pull user/tenant/role out of the Starlette session cookie."""
        role = request.session.get("role") or "viewer"
        # Normalise legacy "tenant" -> "viewer"
        if role == "tenant":
            role = "viewer"

        return AuthContext(
            user_id=request.session.get("user_id"),
            username=request.session.get("username", ""),
            role=role,
            role_level=ROLE_HIERARCHY.get(role, 0),
            tenant_id=request.session.get("tenant_id", "default"),
            selected_tenant=request.session.get("selected_tenant"),
            pending_tenants=request.session.get("pending_tenants", []),
        )

    def _require_context(self) -> AuthContext:
        """Raise if this service was created without a Request."""
        if self.context is None:
            raise RuntimeError(
                f"{type(self).__name__} was not given a Request — "
                "cannot resolve auth context.  Pass request= to the constructor "
                "or override for background-task usage."
            )
        return self.context
