"""add_dashboard_user_role_description

- dashboard_users: 添加 role_description 列
- 只改动这一个列，其他自动检测项留到专用 migration

Revision ID: 3f0cc9249cc4
Revises: d64680f818e8
Create Date: 2026-05-28 05:54:05.439845
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '3f0cc9249cc4'
down_revision: Union[str, None] = 'd64680f818e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('dashboard_users', sa.Column('role_description', sa.String(length=128), nullable=True))


def downgrade() -> None:
    op.drop_column('dashboard_users', 'role_description')
