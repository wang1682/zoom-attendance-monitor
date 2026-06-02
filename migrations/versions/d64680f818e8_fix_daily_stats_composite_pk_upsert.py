"""fix_daily_stats_composite_pk_upsert

- daily_stats: 从 date 单主键改为 (tenant_id, date) 复合主键
- daily_stats: 加 created_at / updated_at 时间戳列
- 所有聚合表确保 upsert 可用

Revision ID: d64680f818e8
Revises: fa8bbcb83c34
Create Date: 2026-05-28 05:48:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = 'd64680f818e8'
down_revision: Union[str, None] = 'fa8bbcb83c34'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ============================================================
    # daily_stats: 重建为复合主键 (tenant_id, date)
    # ============================================================
    # SQLite 不能直接改主键 → 分步重建
    # 1. 重命名旧表
    op.rename_table('daily_stats', 'daily_stats_old')

    # 2. 新建表：复合主键 (tenant_id, date) + created_at/updated_at
    op.create_table('daily_stats',
        sa.Column('tenant_id', sa.String(32), nullable=False),
        sa.Column('date', sa.String(16), nullable=False),
        sa.Column('total_persons', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('total_duration_minutes', sa.Float(), nullable=False, server_default=sa.text('0.0')),
        sa.Column('earliest_entry', sa.String(8), nullable=False, server_default=sa.text("''")),
        sa.Column('latest_entry', sa.String(8), nullable=False, server_default=sa.text("''")),
        sa.Column('unique_emails', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('created_at', sa.DateTime(), nullable=False, server_default=sa.text("(datetime('now'))")),
        sa.Column('updated_at', sa.DateTime(), nullable=False, server_default=sa.text("(datetime('now'))")),
        sa.PrimaryKeyConstraint('tenant_id', 'date'),
    )

    # 3. 数据迁移：旧表 → 新表（跳过主键冲突）
    op.execute("""
        INSERT OR REPLACE INTO daily_stats (tenant_id, date, total_persons, total_duration_minutes, earliest_entry, latest_entry, unique_emails)
        SELECT COALESCE(tenant_id, 'default'), date, total_persons, total_duration_minutes, earliest_entry, latest_entry, unique_emails
        FROM daily_stats_old
    """)

    # 4. 删旧表
    op.drop_table('daily_stats_old')

    # ============================================================
    # person_stats: 为 upsert 加 updated_at 列
    # ============================================================
    # SQLite ALTER TABLE ADD COLUMN 只能加可 NULL 列（不能加 NOT NULL DEFAULT）
    # 两步法：加 nullable 列，再填默认值
    op.execute("ALTER TABLE person_stats ADD COLUMN created_at TIMESTAMP")
    op.execute("ALTER TABLE person_stats ADD COLUMN updated_at TIMESTAMP")
    op.execute("UPDATE person_stats SET created_at = datetime('now'), updated_at = datetime('now')")


def downgrade() -> None:
    # ============================================================
    # daily_stats: 还原为 date 单主键
    # ============================================================
    op.rename_table('daily_stats', 'daily_stats_new')

    op.create_table('daily_stats',
        sa.Column('date', sa.String(16), primary_key=True),
        sa.Column('tenant_id', sa.String(32), nullable=False, server_default=sa.text("'default'")),
        sa.Column('total_persons', sa.Integer(), nullable=False, server_default=sa.text('0')),
        sa.Column('total_duration_minutes', sa.Float(), nullable=False, server_default=sa.text('0.0')),
        sa.Column('earliest_entry', sa.String(8), nullable=False, server_default=sa.text("''")),
        sa.Column('latest_entry', sa.String(8), nullable=False, server_default=sa.text("''")),
        sa.Column('unique_emails', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )

    op.execute("""
        INSERT OR REPLACE INTO daily_stats (date, tenant_id, total_persons, total_duration_minutes, earliest_entry, latest_entry, unique_emails)
        SELECT date, tenant_id, total_persons, total_duration_minutes, earliest_entry, latest_entry, unique_emails
        FROM daily_stats_new
    """)

    op.drop_table('daily_stats_new')

    # ============================================================
    # person_stats: 移除 created_at / updated_at
    # ============================================================
    # SQLite 不支持 DROP COLUMN 的 ALTER TABLE
    # 需要重建表，但降级场景罕见，直接用备用方案
    op.rename_table('person_stats', 'person_stats_old')

    op.create_table('person_stats',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('date', sa.String(16), nullable=False),
        sa.Column('tenant_id', sa.String(32), nullable=False, server_default=sa.text("'default'")),
        sa.Column('name', sa.String(128), nullable=False),
        sa.Column('email', sa.String(256), nullable=False, server_default=sa.text("''")),
        sa.Column('first_entry', sa.String(8), nullable=False, server_default=sa.text("''")),
        sa.Column('last_leave', sa.String(8), nullable=False, server_default=sa.text("''")),
        sa.Column('total_duration_minutes', sa.Float(), nullable=False, server_default=sa.text('0.0')),
        sa.Column('enter_count', sa.Integer(), nullable=False, server_default=sa.text('0')),
    )

    op.execute("""
        INSERT OR REPLACE INTO person_stats (id, date, tenant_id, name, email, first_entry, last_leave, total_duration_minutes, enter_count)
        SELECT id, date, tenant_id, name, email, first_entry, last_leave, total_duration_minutes, enter_count
        FROM person_stats_old
    """)

    op.drop_table('person_stats_old')
