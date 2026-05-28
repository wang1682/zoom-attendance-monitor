"""
database.py — Session 工厂 & 初始化
"""
from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import Session, sessionmaker

from app.settings import settings
from app.models import Base


# 确保 data 目录存在
_db_dir = Path(__file__).parent.parent / "data"
_db_dir.mkdir(parents=True, exist_ok=True)

# ---- Sync Engine（用于脚本 / 迁移） ----
_sync_url = settings.database_url.replace("+aiosqlite", "")
_sync_engine = create_engine(_sync_url, echo=settings.database_echo)
SyncSession = sessionmaker(bind=_sync_engine)

# ---- Async Engine（用于 FastAPI） ----
_engine = create_async_engine(settings.database_url, echo=settings.database_echo)
AsyncSessionLocal = async_sessionmaker(_engine, expire_on_commit=False)


def init_db():
    """通过 Alembic migration 保持 schema 最新"""
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", _sync_url)
    command.upgrade(alembic_cfg, "head")

    # 创建默认租户
    from app.models import Tenant
    with SyncSession() as session:
        existing = session.query(Tenant).filter(Tenant.id == settings.default_tenant_id).first()
        if not existing:
            session.add(Tenant(
                id=settings.default_tenant_id,
                name="default",
                display_name="默认租户",
            ))
            session.commit()


async def get_async_db() -> AsyncGenerator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session


async def init_db_async():
    """异步通过 Alembic migration 保持 schema 最新"""
    from alembic.config import Config
    from alembic import command

    alembic_cfg = Config(str(Path(__file__).parent.parent / "alembic.ini"))
    alembic_cfg.set_main_option("sqlalchemy.url", _sync_url)
    command.upgrade(alembic_cfg, "head")
