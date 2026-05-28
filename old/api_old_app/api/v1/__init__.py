"""
__init__.py — v1 API 注册
"""
from fastapi import APIRouter
from .webhook import router as webhook_router
from .endpoints import router as endpoints_router

router = APIRouter()
router.include_router(webhook_router)
router.include_router(endpoints_router)
