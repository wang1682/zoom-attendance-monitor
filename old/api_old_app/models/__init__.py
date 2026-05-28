"""
models.py — SQLAlchemy ORM 模型
所有表含 tenant_id，支持多租户。
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Float, Text, DateTime, Boolean, ForeignKey, Index, func
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _utcnow():
    return datetime.now(timezone.utc)


def _new_id():
    return uuid.uuid4().hex[:16]


# =====================
# 租户
# =====================

class Tenant(Base):
    __tablename__ = "tenants"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(256), default="")
    plan: Mapped[str] = mapped_column(String(32), default="free")  # free / pro / business
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_global_admin: Mapped[bool] = mapped_column(Boolean, default=False)  # 全局管理员租户
    api_token: Mapped[Optional[str]] = mapped_column(String(64), default=None, unique=True)  # API 访问令牌
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    configs = relationship("TenantConfig", back_populates="tenant", cascade="all, delete-orphan")
    meetings = relationship("MonitoredMeeting", back_populates="tenant", cascade="all, delete-orphan")


class TenantConfig(Base):
    """租户级配置（Zoom 凭证、时段等，替代 .env 中租户相关的部分）"""
    __tablename__ = "tenant_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_tenant_config_key", "tenant_id", "key", unique=True),
    )

    tenant = relationship("Tenant", back_populates="configs")


# =====================
# 会议室
# =====================

class MonitoredMeeting(Base):
    """被监控的会议室"""
    __tablename__ = "monitored_meetings"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_new_id)
    tenant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meeting_id: Mapped[str] = mapped_column(String(64), nullable=False)
    label: Mapped[str] = mapped_column(String(128), default="")
    meeting_type: Mapped[str] = mapped_column(String(32), default="zoom")  # zoom / meet / teams / feishu
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_monitored_meeting_meeting_id", "tenant_id", "meeting_id", unique=True),
    )

    tenant = relationship("Tenant", back_populates="meetings")
    participants = relationship("ParticipantEvent", back_populates="meeting", cascade="all, delete-orphan")


# =====================
# 事件表
# =====================

class ParticipantEvent(Base):
    """参会事件（enter / leave），Webhook + Poll 双源写入"""
    __tablename__ = "participant_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(
        String(32), ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    meeting_id_ref: Mapped[str] = mapped_column(
        String(32), ForeignKey("monitored_meetings.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(256), default="", index=True)
    action: Mapped[str] = mapped_column(String(16), nullable=False)  # enter / leave
    action_time: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), default="poll")  # poll / webhook
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)

    __table_args__ = (
        Index("ix_participant_events_lookup", "tenant_id", "action_time", "name"),
    )

    meeting = relationship("MonitoredMeeting", back_populates="participants")


class WebhookEvent(Base):
    """Zoom Webhook 原始事件落库"""
    __tablename__ = "webhook_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), default="default", index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    processed: Mapped[bool] = mapped_column(Boolean, default=False)
    received_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    processed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)


class SeenEmail(Base):
    """陌生邮箱追踪"""
    __tablename__ = "seen_emails"

    email: Mapped[str] = mapped_column(String(256), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(String(32), default="default", index=True)
    name: Mapped[str] = mapped_column(String(128), default="")
    first_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)


class DailyStat(Base):
    """每日统计汇总（复合主键 upsert-ready）"""
    __tablename__ = "daily_stats"

    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    date: Mapped[str] = mapped_column(String(16), primary_key=True)
    total_persons: Mapped[int] = mapped_column(Integer, default=0)
    total_duration_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    earliest_entry: Mapped[str] = mapped_column(String(8), default="")
    latest_entry: Mapped[str] = mapped_column(String(8), default="")
    unique_emails: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)


class PersonStat(Base):
    """每人每日统计"""
    __tablename__ = "person_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), default="default", index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    email: Mapped[str] = mapped_column(String(256), default="")
    first_entry: Mapped[str] = mapped_column(String(8), default="")
    last_leave: Mapped[str] = mapped_column(String(8), default="")
    total_duration_minutes: Mapped[float] = mapped_column(Float, default=0.0)
    enter_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow, onupdate=_utcnow)

    __table_args__ = (
        Index("ix_person_stats_date_name", "tenant_id", "date", "name", unique=True),
    )


class HourlyActivity(Base):
    """逐时活跃度"""
    __tablename__ = "hourly_activity"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    date: Mapped[str] = mapped_column(String(16), nullable=False)
    tenant_id: Mapped[str] = mapped_column(String(32), default="default", index=True)
    hour: Mapped[int] = mapped_column(Integer, nullable=False)
    person_count: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_hourly_activity_lookup", "tenant_id", "date", "hour", unique=True),
    )


class Alert(Base):
    """告警记录"""
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), default="default", index=True)
    alert_type: Mapped[str] = mapped_column(String(64), nullable=False)  # stranger / overtime / anomaly
    severity: Mapped[str] = mapped_column(String(16), default="info")    # info / warning / critical
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    related_name: Mapped[str] = mapped_column(String(128), default="")
    related_email: Mapped[str] = mapped_column(String(256), default="")
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
