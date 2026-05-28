"""
phase2/db.py — Phase 2 数据库初始化 & Session
"""
from __future__ import annotations

import os
from pathlib import Path
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from phase2.models import Base, Tenant, ZoomAccount, ZoomMeeting, TelegramChannel, DashboardUser
from app.settings import settings

# DB 路径（和旧库共用同一文件，但不冲突）
DATA_DIR = Path(__file__).parent.parent / "data"
DATA_DIR.mkdir(exist_ok=True)
DB_PATH = str(DATA_DIR / "tracking.db")

engine = create_engine(
    f"sqlite:///{DB_PATH}",
    connect_args={"check_same_thread": False},
    echo=settings.database_echo,
)

SyncSession = sessionmaker(bind=engine)


def new_id() -> str:
    return uuid4().hex[:16]


def init_db():
    """建表 + 种子数据"""
    Base.metadata.create_all(engine)
    _seed_default_tenant()


def _seed_default_tenant():
    """确保有默认租户 + 基本配置（无条件写入，幂等）"""
    with SyncSession() as s:
        # 租户
        tenant = s.query(Tenant).filter_by(id="default").first()
        if not tenant:
            s.add(Tenant(id="default", name="default", display_name="默认租户", plan="pro"))
            s.flush()

        # Zoom 账号
        if settings.zoom_account_id:
            existing = s.query(ZoomAccount).filter_by(
                tenant_id="default", account_id=settings.zoom_account_id
            ).first()
            if not existing:
                s.add(ZoomAccount(
                    id=new_id(), tenant_id="default",
                    label="主账号",
                    account_id=settings.zoom_account_id,
                    client_id=settings.zoom_client_id,
                    client_secret=settings.zoom_client_secret,
                    host_email=settings.zoom_host_email,
                ))
                s.flush()

        # 会议室
        for mid in settings.all_meeting_ids:
            existing = s.query(ZoomMeeting).filter_by(
                tenant_id="default", meeting_id=mid
            ).first()
            if not existing:
                s.add(ZoomMeeting(
                    id=new_id(), tenant_id="default",
                    meeting_id=mid,
                    label="自习室(PMI)" if mid == settings.zoom_pmi_id else f"会议{mid[-4:]}",
                    meeting_type="pmi" if mid == settings.zoom_pmi_id else "scheduled",
                ))

        # Telegram 频道
        cid = settings.telegram_private_chat_id
        if cid:
            existing = s.query(TelegramChannel).filter_by(
                tenant_id="default", chat_id=cid
            ).first()
            if not existing:
                s.add(TelegramChannel(
                    id=new_id(), tenant_id="default",
                    chat_id=cid, label="私聊", is_group=False,
                ))
        gid = settings.telegram_group_chat_id
        if gid:
            existing = s.query(TelegramChannel).filter_by(
                tenant_id="default", chat_id=gid
            ).first()
            if not existing:
                s.add(TelegramChannel(
                    id=new_id(), tenant_id="default",
                    chat_id=gid, label="群组", is_group=True,
                ))
        # DashboardUser — 创建默认 owner（如果 .env admin user 为空则跳过）
        if settings.dashboard_admin_user:
            existing_owner = s.query(DashboardUser).filter_by(
                tenant_id="default", username=settings.dashboard_admin_user
            ).first()
            if not existing_owner:
                import bcrypt
                pw_hash = settings.dashboard_admin_password_hash
                if not pw_hash:
                    # 生成 hash
                    pw_hash = bcrypt.hashpw(b"admin123", bcrypt.gensalt()).decode()
                s.add(DashboardUser(
                    id=new_id(), tenant_id="default",
                    username=settings.dashboard_admin_user,
                    password_hash=pw_hash,
                    role="owner",
                    role_description="系统管理员",
                    active=True,
                ))
        s.commit()
