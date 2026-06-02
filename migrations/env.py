"""Alembic env.py — 合并 app.models + phase2.models + analytics.models metadata"""
import sys
from pathlib import Path

_project_root = Path(__file__).parent.parent
if str(_project_root) not in sys.path:
    sys.path.insert(0, str(_project_root))

from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool
from alembic import context

# 导入所有模型 — import 触发 registry
from app.models import Base as AppBase
from phase2.models import Base as Phase2Base
from app.analytics import models as analytics_models  # noqa — 触发 analytics 表注册到 AppBase
from app.settings import settings

# 将 phase2 的所有表合并到 app 的 metadata 中
for table_name, table in Phase2Base.metadata.tables.items():
    if table_name not in AppBase.metadata.tables:
        table.to_metadata(AppBase.metadata)

target_metadata = AppBase.metadata

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Alembic 使用 sync driver，去掉 +aiosqlite
_sync_url = settings.database_url.replace("+aiosqlite", "")
config.set_main_option("sqlalchemy.url", _sync_url)


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(url=url, target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
