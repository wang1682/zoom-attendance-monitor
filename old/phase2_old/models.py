"""
phase2/models.py — Phase 2 数据资产化专用 ORM 模型

所有表预留 tenant_id，为多客户分租户做好准备。
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import (
    Column, String, Integer, Boolean, Float, DateTime, Text,
    ForeignKey, JSON, create_engine, UniqueConstraint, Index,
)
from sqlalchemy.orm import declarative_base, relationship

Base = declarative_base()
utc_now = lambda: datetime.now(timezone.utc)


class Tenant(Base):
    """租户"""
    __tablename__ = "tenants"

    id = Column(String(32), primary_key=True)
    name = Column(String(128), nullable=False)
    display_name = Column(String(256), default="")
    plan = Column(String(32), default="free")       # free / pro / business
    active = Column(Boolean, default=True)
    is_global_admin = Column(Boolean, default=False)  # 全局管理员租户（可管理其他租户）
    api_token = Column(String(64), default=None)      # API 访问令牌
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


# ─── Dashboard 用户 ─────────────────────────

# ─── 角色层级（数字越大权限越高） ──────────
ROLE_HIERARCHY = {
    "owner":   100,
    "admin":   80,
    "analyst": 60,
    "viewer":  40,
}

# 角色 → 可见 nav 项（分层结构，直接对应模板）
NAV_DEFINITIONS = {
    "dashboard":  { "label": "总览", "section": "总览", "icon": "M3 12l2-2m0 0l7-7 7 7M5 10v10a1 1 0 001 1h3m10-11l2 2m-2-2v10a1 1 0 01-1 1h-3m-6 0a1 1 0 001-1v-4a1 1 0 011-1h2a1 1 0 011 1v4a1 1 0 001 1m-6 0h6" },
    "events":      { "label": "Webhook 事件", "section": "总览", "icon": "M9 5H7a2 2 0 00-2 2v12a2 2 0 002 2h10a2 2 0 002-2V7a2 2 0 00-2-2h-2M9 5a2 2 0 002 2h2a2 2 0 002-2M9 5a2 2 0 012-2h2a2 2 0 012 2m-3 7h3m-3 4h3m-6-4h.01M9 16h.01" },
    "participants": { "label": "参会记录", "section": "总览", "icon": "M17 20h5v-2a3 3 0 00-5.356-1.857M17 20H7m10 0v-2c0-.656-.126-1.283-.356-1.857M7 20H2v-2a3 3 0 015.356-1.857M7 20v-2c0-.656.126-1.283.356-1.857m0 0a5.002 5.002 0 019.288 0M15 7a3 3 0 11-6 0 3 3 0 016 0zm6 3a2 2 0 11-4 0 2 2 0 014 0zM7 10a2 2 0 11-4 0 2 2 0 014 0z" },
    "alerts":      { "label": "预警日志", "section": "总览", "icon": "M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" },
    "summary":     { "label": "日报汇总", "section": "总览", "icon": "M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" },
    "analytics":   { "label": "分析总览", "section": "智能分析", "icon": "M13 7h8m0 0v8m0-8l-8 8-4-4-6 6" },
    "analytics_daily":  { "label": "每日聚合", "section": "智能分析", "icon": "M9 19v-6a2 2 0 00-2-2H5a2 2 0 00-2 2v6a2 2 0 002 2h2a2 2 0 002-2zm0 0V9a2 2 0 012-2h2a2 2 0 012 2v10m-6 0a2 2 0 002 2h2a2 2 0 002-2m0 0V5a2 2 0 012-2h2a2 2 0 012 2v14a2 2 0 01-2 2h-2a2 2 0 01-2-2z" },
    "analytics_risks":  { "label": "风险记录", "section": "智能分析", "icon": "M12 9v2m0 4h.01m-6.938 4h13.856c1.54 0 2.502-1.667 1.732-2.5L13.732 4c-.77-.833-1.964-.833-2.732 0L4.082 16.5c-.77.833.192 2.5 1.732 2.5z" },
    "analytics_reports": { "label": "AI 报告", "section": "智能分析", "icon": "M9 12h6m-6 4h6m2 5H7a2 2 0 01-2-2V5a2 2 0 012-2h5.586a1 1 0 01.707.293l5.414 5.414a1 1 0 01.293.707V19a2 2 0 01-2 2z" },
    "settings":    { "label": "系统设置", "section": "配置中心", "icon": "M10.325 4.317c.426-1.756 2.924-1.756 3.35 0a1.724 1.724 0 002.573 1.066c1.543-.94 3.31.826 2.37 2.37a1.724 1.724 0 001.066 2.573c1.756.426 1.756 2.924 0 3.35a1.724 1.724 0 00-1.066 2.573c.94 1.543-.826 3.31-2.37 2.37a1.724 1.724 0 00-2.573 1.066c-.426 1.756-2.924 1.756-3.35 0a1.724 1.724 0 00-2.573-1.066c-1.543.94-3.31-.826-2.37-2.37a1.724 1.724 0 00-1.066-2.573c-1.756-.426-1.756-2.924 0-3.35a1.724 1.724 0 001.066-2.573c-.94-1.543.826-3.31 2.37-2.37.996.608 2.296.07 2.572-1.065z" },
    "rules":       { "label": "预警规则", "section": "配置中心", "icon": "M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 9-11.622 0-1.042-.133-2.052-.382-3.016z" },
    "channels":    { "label": "通知渠道", "section": "配置中心", "icon": "M8 10h.01M12 10h.01M16 10h.01M9 16H5a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v8a2 2 0 01-2 2h-5l-5 5v-5z" },
    "admin":       { "label": "后台管理", "section": "配置中心", "icon": "M12 4.354a4 4 0 110 5.292M15 21H3v-1a6 6 0 0112 0v1zm0 0h6v-1a6 6 0 00-9-5.197m13.5-9a2.5 2.5 0 11-5 0 2.5 2.5 0 015 0z" },
}

# 扁平 key 列表：角色 → 可见条目（含子条目 "analytics_daily" 等）
NAV_URL_MAP = {
    "analytics_daily":  "/dashboard/analytics/daily",
    "analytics_risks":  "/dashboard/analytics/risks",
    "analytics_reports": "/dashboard/analytics/reports",
}

ROLE_NAV = {
    "owner":   ["dashboard", "events", "participants", "alerts",
                 "analytics", "analytics_daily", "analytics_risks", "analytics_reports",
                 "summary", "settings", "rules", "channels", "admin"],
    "admin":   ["dashboard", "events", "participants", "alerts",
                "analytics", "analytics_daily", "analytics_risks", "analytics_reports",
                "summary", "settings", "rules", "channels"],
    "analyst": ["dashboard", "events", "participants", "alerts",
                "analytics", "analytics_daily", "analytics_risks", "analytics_reports",
                "summary"],
    "viewer":  ["dashboard", "events", "participants", "alerts",
                "summary"],
}

def build_nav_items(role: str, active: str = "") -> list:
    """将 ROLE_NAV 扁平列表转为模板所需的 [{label, items}] 结构。"""
    keys = ROLE_NAV.get(role, ROLE_NAV["viewer"])
    sections = {}
    for key in keys:
        ndef = NAV_DEFINITIONS.get(key)
        if not ndef:
            continue
        sec = ndef["section"]
        if sec not in sections:
            sections[sec] = {"label": sec, "items": []}
        url = NAV_URL_MAP.get(key, f"/dashboard/{key.replace('_','/')}")
        sections[sec]["items"].append({
            "title": ndef["label"],
            "url": url,
            "icon": ndef["icon"],
            "active": (active == key or active == url.rstrip("/")),
        })
    return list(sections.values())

# 角色 → 可管理的作用域: settings/rules/channels/users/accounts/tenants
ROLE_SCOPES = {
    "owner":   {"settings", "rules", "channels", "users", "accounts", "tenants", "api_token"},
    "admin":   {"settings", "rules", "channels"},
    "analyst": set(),
    "viewer":  set(),
}


class DashboardUser(Base):
    """Dashboard 后台用户（非 .env 管理，支持多租户）"""
    __tablename__ = "dashboard_users"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=False, index=True)

    username = Column(String(128), nullable=False, unique=True, index=True)
    password_hash = Column(String(256), nullable=False)
    display_name = Column(String(256), default="")
    role = Column(String(16), default="owner")     # owner / admin / analyst / viewer
    role_description = Column(String(128), default="")  # 角色中文描述（可选）
    active = Column(Boolean, default=True)

    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        Index("idx_duser_tenant", "tenant_id"),
    )


class ZoomAccount(Base):
    """Zoom 账号凭证（每个租户可配多个 Zoom 账号）"""
    __tablename__ = "zoom_accounts"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=False, index=True)
    label = Column(String(128), default="")          # 备注
    account_id = Column(String(256), nullable=False)
    client_id = Column(String(256), nullable=False)
    client_secret = Column(String(512), nullable=False)
    host_email = Column(String(256), default="")
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class ZoomMeeting(Base):
    """被监控的 Zoom 会议室"""
    __tablename__ = "zoom_meetings"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=False, index=True)
    zoom_account_id = Column(String(32), ForeignKey("zoom_accounts.id"), nullable=True)
    meeting_id = Column(String(64), nullable=False, index=True)    # Zoom 会议号/PMI
    label = Column(String(256), default="")                        # 会议名称
    meeting_type = Column(String(32), default="pmi")               # pmi / scheduled / recurring
    active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)


class ZoomParticipant(Base):
    """参会记录（Monitor 轮询写入）"""
    __tablename__ = "zoom_participants"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=False, index=True)

    meeting_id = Column(String(64), nullable=False, index=True)
    name = Column(String(256), nullable=False, index=True)
    email = Column(String(256), default="")
    action = Column(String(16), nullable=False)        # enter / leave
    action_time = Column(DateTime, nullable=False, index=True)
    duration_seconds = Column(Integer, default=0)      # 本次停留秒数
    source = Column(String(16), default="poll")        # poll / webhook

    created_at = Column(DateTime, default=utc_now)

    __table_args__ = (
        Index("idx_zpart_tenant_time", "tenant_id", "action_time"),
    )


class ZoomEvent(Base):
    """Zoom Webhook 事件（Webhook 写入）"""
    __tablename__ = "zoom_events"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=False, index=True)

    event_type = Column(String(128), nullable=False, index=True)   # meeting.participant_joined 等
    meeting_id = Column(String(64), nullable=True, index=True)
    participant_name = Column(String(256), default="")
    participant_email = Column(String(256), default="")
    raw_payload = Column(JSON, default=dict)                       # 原始事件完整内容
    processed = Column(Boolean, default=False)                     # 是否已被消费

    received_at = Column(DateTime, default=utc_now, index=True)
    processed_at = Column(DateTime, nullable=True)

    __table_args__ = (
        Index("idx_zevent_tenant_time", "tenant_id", "received_at"),
    )


class AlertRule(Base):
    """预警规则"""
    __tablename__ = "alert_rules"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=False, index=True)

    name = Column(String(128), nullable=False)
    event_type = Column(String(128), default="*")       # 匹配事件类型，* 为全部
    severity = Column(String(16), default="info")       # info / warning / critical
    enabled = Column(Boolean, default=True)
    created_at = Column(DateTime, default=utc_now)


class AlertLog(Base):
    """推送日志 — 每次 Telegram / 其他渠道的推送记录"""
    __tablename__ = "alert_logs"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=False, index=True)

    channel = Column(String(32), default="telegram")    # telegram / email / webhook
    message_type = Column(String(64), default="")       # event / summary / daily / stranger
    title = Column(String(256), default="")
    content = Column(Text, default="")
    recipient = Column(String(256), default="")         # chat_id, email 等
    success = Column(Boolean, default=True)
    error_message = Column(Text, default="")

    sent_at = Column(DateTime, default=utc_now, index=True)

    __table_args__ = (
        Index("idx_alertlog_tenant_time", "tenant_id", "sent_at"),
    )


class AuditLog(Base):
    """审计日志 — 记录登录/登出等安全事件"""
    __tablename__ = "audit_logs"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=True, index=True)

    action = Column(String(32), nullable=False, index=True)    # login_success / login_failed / logout
    username = Column(String(128), default="")
    ip_address = Column(String(64), default="")
    user_agent = Column(String(512), default="")
    detail = Column(String(1024), default="")                  # 失败原因等

    occurred_at = Column(DateTime, default=utc_now, index=True)
    created_at = Column(DateTime, default=utc_now)


class SystemSetting(Base):
    """系统配置 — key-value 存储，UI 可修改"""
    __tablename__ = "system_settings"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=False, index=True)

    key = Column(String(128), nullable=False)
    value = Column(Text, default="")
    description = Column(String(256), default="")
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)

    __table_args__ = (
        UniqueConstraint("tenant_id", "key", name="uq_sys_setting_key"),
    )


class TelegramChannel(Base):
    """Telegram 推送配置"""
    __tablename__ = "telegram_channels"

    id = Column(String(32), primary_key=True)
    tenant_id = Column(String(32), ForeignKey("tenants.id"), nullable=False, index=True)

    chat_id = Column(String(64), nullable=False)
    label = Column(String(128), default="")            # "私聊" / "自习室群" 等
    enabled = Column(Boolean, default=True)
    is_group = Column(Boolean, default=False)
    created_at = Column(DateTime, default=utc_now)
