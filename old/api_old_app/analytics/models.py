"""analytics/models.py — 分析层 ORM 模型
新增表：
- participant_daily_stats: 人天级详情（含风险标记）
- risk_scores: 风险记录
- ai_reports: AI 报告归档

已有的 daily_stats / person_stats / hourly_activity 在 app/models/__init__.py
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, Boolean, ForeignKey, Index, func
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models import Base, _utcnow


def _new_id() -> str:
    return uuid.uuid4().hex[:16]


class ParticipantDailyStat(Base):
    """人天级聚合 — 比 person_stats 更丰富，含风险标记"""
    __tablename__ = "participant_daily_stats"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), default="default", nullable=False, index=True)
    date: Mapped[str] = mapped_column(String(16), nullable=False)  # "2026-05-28"
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(256), default="")
    first_entry: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    last_leave: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    total_duration_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    enter_count: Mapped[int] = mapped_column(Integer, default=0)
    short_stay: Mapped[bool] = mapped_column(Boolean, default=False)       # 停留 <3min
    late_entry: Mapped[bool] = mapped_column(Boolean, default=False)       # 晚于截止时间
    night_entry: Mapped[bool] = mapped_column(Boolean, default=False)      # 23:00-06:00 进入
    consecutive_late_days: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_pds_tenant_date_name", "tenant_id", "date", "name", unique=True),
    )


class RiskScore(Base):
    """风险记录"""
    __tablename__ = "risk_scores"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), default="default", nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(256), default="")
    risk_type: Mapped[str] = mapped_column(String(32), nullable=False)   # late / short_stay / night_entry / low_activity / consecutive_late
    severity: Mapped[str] = mapped_column(String(16), default="medium")  # low / medium / high / critical
    description: Mapped[str] = mapped_column(Text, default="")
    date: Mapped[str] = mapped_column(String(16), nullable=False)         # 风险发生的日期
    dismissed: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("ix_risk_dedup", "tenant_id", "name", "risk_type", "date", unique=True),
    )


class AiReport(Base):
    """AI 报告归档"""
    __tablename__ = "ai_reports"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), default="default", nullable=False, index=True)
    report_type: Mapped[str] = mapped_column(String(32), nullable=False)  # daily / weekly / risk_summary
    date: Mapped[str] = mapped_column(String(16), nullable=False)         # 报告覆盖日期
    title: Mapped[str] = mapped_column(String(256), default="")
    summary: Mapped[str] = mapped_column(Text, default="")                # AI 生成的内容
    metrics: Mapped[str] = mapped_column(Text, default="{}")              # JSON: 关键指标快照
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    ai_provider: Mapped[str] = mapped_column(String(32), default="deepseek")

    __table_args__ = (
        Index("ix_ai_report_type_date", "tenant_id", "report_type", "date", unique=True),
    )
