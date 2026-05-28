#!/usr/bin/env python3
"""
start_monitor.py — Zoom 轮询监控服务（后台运行）
"""
import asyncio
import sys
import os

# 确保能 import app 和 phase2
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.services.monitor_service import monitor_loop
from phase2 import init_db as init_phase2_db


async def main():
    # 初始化 Phase 2 表
    init_phase2_db()
    try:
        await monitor_loop()
    except KeyboardInterrupt:
        sys.stdout.write("\n[MONITOR] 已停止\n")
        sys.stdout.flush()


if __name__ == "__main__":
    asyncio.run(main())
