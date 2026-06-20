"""Zoom Monitor Service Layer.

All business logic centralized in Service classes.
Routes/Pages/Bots call services, never make role/tenant decisions themselves.
"""

from services.base import BaseService, AuthContext
from services.auth import AuthService, ROLE_HIERARCHY, role_ge

__all__ = [
    "BaseService", "AuthContext",
    "AuthService", "ROLE_HIERARCHY", "role_ge",
]
