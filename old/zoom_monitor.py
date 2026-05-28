#!/usr/bin/env python3
"""
zoom_monitor.py — Zoom Monitor 兼容入口
保持旧路径兼容，实际引导到新模块
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.settings import settings
from app.database import init_db


def main():
    # 校验
    settings.validate_required()

    # 初始化 DB（建表 + 默认租户）
    init_db()

    # 启动监控
    from app.services.monitor_service import monitor_loop
    import asyncio
    asyncio.run(monitor_loop())


if __name__ == "__main__":
    main()
