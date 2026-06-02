"""zoom_monitor_p8_m004_tenant_api_token.py
加 api_token 列到 tenants 表
"""
from __future__ import annotations

from sqlalchemy import text

from app.database import SyncSession


def upgrade():
    with SyncSession() as s:
        # 检测列是否存在
        try:
            s.execute(text("SELECT api_token FROM tenants LIMIT 0"))
            print("[SKIP] api_token 列已存在")
            return
        except Exception:
            pass

        s.execute(text("ALTER TABLE tenants ADD COLUMN api_token VARCHAR(64)"))
        s.commit()
        print("[MIGRATE] 已添加 api_token 列")


if __name__ == "__main__":
    upgrade()
