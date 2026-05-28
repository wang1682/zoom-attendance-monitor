"""
phase2/__init__.py — Phase 2 模块入口

调用 init_phase2() 完成表创建 + 种子数据
"""
from phase2.db import init_db

__all__ = ["init_db"]
