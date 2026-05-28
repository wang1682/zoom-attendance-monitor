"""
phase2/config_api.py — Phase 5 产品配置中心 API

提供:
- GET /api/v2/config/settings — 读取系统设置
- PUT /api/v2/config/settings — 更新系统设置
- GET /api/v2/config/rules — 预警规则列表
- PUT /api/v2/config/rules/{rule_id} — 更新单条规则开关
- GET /api/v2/config/channels — Telegram 频道列表
- PUT /api/v2/config/channels/{channel_id} — 更新频道配置（enable/disable/label）
- POST /api/v2/config/channels/test — 测试发送（试发消息）
- POST /api/v2/config/api-token/reset — 重置 API Token
"""
from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4
import secrets
import string

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from phase2.db import SyncSession, new_id
from phase2.models import (
    SystemSetting, AlertRule, TelegramChannel, ZoomAccount, ZoomMeeting,
)
from phase2.auth import get_current_user, check_role
from phase2.models import ROLE_HIERARCHY
from phase2.audit import log_audit
from app.integrations.telegram import TelegramNotifier
from app.settings import settings

router = APIRouter(prefix="/api/v2/config", tags=["phase5-config"])


# ─── 依赖 ─────────────────────────────

def _require_auth(request: Request):
    user = get_current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return user


# ─── 工具 ─────────────────────────────

def _mask(s: str, show: int = 4) -> str:
    """脱敏显示：只显示前后 show 位"""
    if len(s) <= show * 2:
        return s[:show] + "***"
    return s[:show] + "..." + s[-show:]


DEFAULT_SETTINGS = {
    "push_start_hour": {"value": "7", "description": "推送开始小时（0-23）"},
    "push_end_hour": {"value": "23", "description": "推送结束小时（0-23）"},
    "signin_deadline_hour": {"value": "9", "description": "签到截止小时（0-23）"},
    "meeting_auto_start": {"value": "true", "description": "会议开始时自动发送通知"},
}

DEFAULT_ALERT_RULES = [
    ("late_arrival", "迟到预警"),
    ("stranger_email", "陌生邮箱预警"),
    ("short_visit", "短时参加预警"),
    ("empty_meeting", "无人时段过长预警"),
    ("meeting_start", "会议开始通知"),
    ("meeting_end", "会议结束通知"),
    ("participant_enter", "加入通知"),
    ("participant_leave", "离开通知"),
]


def _seed_if_empty():
    """如果配置为空则写入默认值（幂等）"""
    with SyncSession() as s:
        # system settings
        for key, meta in DEFAULT_SETTINGS.items():
            existing = s.query(SystemSetting).filter_by(
                tenant_id="default", key=key
            ).first()
            if not existing:
                s.add(SystemSetting(
                    id=new_id(), tenant_id="default",
                    key=key, value=meta["value"],
                    description=meta["description"],
                ))

        # alert rules
        existing_rules = s.query(AlertRule).filter_by(tenant_id="default").count()
        if existing_rules == 0:
            for rname, rdesc in DEFAULT_ALERT_RULES:
                s.add(AlertRule(
                    id=new_id(), tenant_id="default",
                    name=rdesc, event_type=rname,
                    severity="warning", enabled=True,
                ))

        s.commit()


# ─── Settings ─────────────────────────

@router.get("/settings")
async def get_settings(request: Request, auth_user: str = Depends(_require_auth)):
    """读取系统设置"""
    _seed_if_empty()
    with SyncSession() as s:
        rows = s.query(SystemSetting).filter_by(tenant_id="default").all()

    settings_map = {r.key: r for r in rows}
    result = {}
    for key, meta in DEFAULT_SETTINGS.items():
        if key in settings_map:
            r = settings_map[key]
            result[key] = {
                "value": r.value,
                "description": r.description,
                "updated_at": r.updated_at.isoformat() if r.updated_at else "",
            }
        else:
            result[key] = {"value": meta["value"], "description": meta["description"], "updated_at": ""}

    # Zoom 状态（脱敏）
    zoom = _zoom_status()
    tg = _telegram_status()

    # API Token 状态（脱敏）
    api_token = ""
    if settings.api_token:
        api_token = _mask(settings.api_token)

    return {
        "settings": result,
        "zoom": zoom,
        "telegram": tg,
        "api_token": api_token,
    }


class UpdateSettingsBody(BaseModel):
    settings: dict[str, str]  # key → value


@router.put("/settings")
async def update_settings(
    body: UpdateSettingsBody,
    request: Request,
    auth_user: str = Depends(_require_auth),
):
    """更新系统设置（只更新传过来的 key）"""
    # 写操作：至少需要 admin 角色
    role_err = check_role(auth_user, "admin")
    if role_err:
        raise HTTPException(status_code=403, detail=role_err)
    _seed_if_empty()
    changed = []
    with SyncSession() as s:
        for key, value in body.settings.items():
            existing = s.query(SystemSetting).filter_by(
                tenant_id="default", key=key
            ).first()
            if existing:
                if existing.value != value:
                    existing.value = value
                    existing.updated_at = datetime.now(timezone.utc)
                    changed.append(key)
            else:
                s.add(SystemSetting(
                    id=new_id(), tenant_id="default",
                    key=key, value=value,
                    description=DEFAULT_SETTINGS.get(key, {}).get("description", ""),
                ))
                changed.append(key)
        s.commit()

    if changed:
        log_audit("config_update", username=auth_user, request=request,
                   detail=f"settings changed: {', '.join(changed)}")

    return {"status": "ok", "updated": changed}


# ─── Alert Rules ────────────────────────

@router.get("/rules")
async def get_rules(request: Request, auth_user: str = Depends(_require_auth)):
    """预警规则列表"""
    _seed_if_empty()
    with SyncSession() as s:
        rules = s.query(AlertRule).filter_by(tenant_id="default").order_by(
            AlertRule.created_at
        ).all()
    return [
        {
            "id": r.id,
            "name": r.name,
            "event_type": r.event_type,
            "severity": r.severity,
            "enabled": bool(r.enabled),
        }
        for r in rules
    ]


class UpdateRuleBody(BaseModel):
    enabled: bool


@router.put("/rules/{rule_id}")
async def update_rule(
    rule_id: str,
    body: UpdateRuleBody,
    request: Request,
    auth_user: str = Depends(_require_auth),
):
    """更新单条预警规则"""
    # 写操作：至少需要 admin 角色
    role_err = check_role(auth_user, "admin")
    if role_err:
        raise HTTPException(status_code=403, detail=role_err)
    with SyncSession() as s:
        rule = s.query(AlertRule).filter_by(
            id=rule_id, tenant_id="default"
        ).first()
        if not rule:
            raise HTTPException(status_code=404, detail="Rule not found")
        rule_name = rule.name
        old_enabled = bool(rule.enabled)
        rule.enabled = body.enabled
        s.commit()

    state = "enabled" if body.enabled else "disabled"
    log_audit("rule_update", username=auth_user, request=request,
               detail=f"rule '{rule_name}': {state}")
    return {"status": "ok", "id": rule_id, "enabled": body.enabled}


# ─── Telegram Channels ─────────────────

@router.get("/channels")
async def get_channels(request: Request, auth_user: str = Depends(_require_auth)):
    """Telegram 频道列表"""
    with SyncSession() as s:
        channels = s.query(TelegramChannel).filter_by(
            tenant_id="default"
        ).order_by(TelegramChannel.created_at).all()
    return [
        {
            "id": c.id,
            "chat_id": c.chat_id,
            "label": c.label or "",
            "enabled": bool(c.enabled),
            "is_group": bool(c.is_group),
        }
        for c in channels
    ]


class UpdateChannelBody(BaseModel):
    enabled: bool | None = None
    label: str | None = None


@router.put("/channels/{channel_id}")
async def update_channel(
    channel_id: str,
    body: UpdateChannelBody,
    request: Request,
    auth_user: str = Depends(_require_auth),
):
    """更新频道配置"""
    # 写操作：至少需要 admin 角色
    role_err = check_role(auth_user, "admin")
    if role_err:
        raise HTTPException(status_code=403, detail=role_err)
    with SyncSession() as s:
        ch = s.query(TelegramChannel).filter_by(
            id=channel_id, tenant_id="default"
        ).first()
        if not ch:
            raise HTTPException(status_code=404, detail="Channel not found")

        changes = []
        if body.enabled is not None:
            old = bool(ch.enabled)
            ch.enabled = body.enabled
            changes.append(f"enabled={body.enabled}")
        if body.label is not None:
            ch.label = body.label
            changes.append(f"label='{body.label}'")
        s.commit()

    if changes:
        log_audit("channel_update", username=auth_user, request=request,
                   detail=f"channel {ch.chat_id}: {', '.join(changes)}")
    return {"status": "ok"}


class TestChannelBody(BaseModel):
    channel_id: str


@router.post("/channels/test")
async def test_channel(
    body: TestChannelBody,
    request: Request,
    auth_user: str = Depends(_require_auth),
):
    """测试发送一条 Telegram 消息"""
    # 写操作：至少需要 admin 角色
    role_err = check_role(auth_user, "admin")
    if role_err:
        raise HTTPException(status_code=403, detail=role_err)
    with SyncSession() as s:
        ch = s.query(TelegramChannel).filter_by(
            id=body.channel_id, tenant_id="default"
        ).first()
        if not ch:
            raise HTTPException(status_code=404, detail="Channel not found")
        chat_id = ch.chat_id

    tg = TelegramNotifier()
    ok = await tg.send(
        chat_id=chat_id,
        text="✅ **配置中心测试消息**\n\n如果你的 Bot 能收到这条消息，说明配置正确。",
    )

    log_audit("channel_test", username=auth_user, request=request,
               detail=f"test send to {chat_id}: {'ok' if ok else 'failed'}")
    return {"status": "ok" if ok else "error", "detail": "Message sent" if ok else "发送失败"}


# ─── API Token ────────────────────────

@router.post("/api-token/reset")
async def reset_api_token(
    request: Request,
    auth_user: str = Depends(_require_auth),
):
    """重置 API Token（生成新随机值，旧 token 立即失效）"""
    # 安全敏感操作：仅 owner
    role_err = check_role(auth_user, "owner")
    if role_err:
        raise HTTPException(status_code=403, detail=role_err)
    new_token = "zmt_" + "".join(
        secrets.choice(string.ascii_letters + string.digits) for _ in range(32)
    )

    env_path = "/opt/zoom-monitor/.env"  # 明确路径
    import os
    import re

    if os.path.exists(env_path):
        with open(env_path) as f:
            content = f.read()
        if "API_TOKEN=" in content:
            content = re.sub(r"API_TOKEN=.*", f"API_TOKEN={new_token}", content)
        else:
            content += f"\nAPI_TOKEN={new_token}\n"
        with open(env_path, "w") as f:
            f.write(content)

        # 运行时更新 settings 对象
        settings.api_token = new_token

        log_audit("api_token_reset", username=auth_user, request=request)
        return {"status": "ok", "api_token": _mask(new_token)}

    log_audit("api_token_reset_failed", username=auth_user, request=request,
               detail="could not find .env file")
    raise HTTPException(status_code=500, detail="Could not write .env file")


# ─── 辅助：Zoom / Telegram 状态 ────

def _zoom_status() -> dict:
    with SyncSession() as s:
        acc = s.query(ZoomAccount).filter_by(tenant_id="default").first()
    if not acc:
        return {"connected": False, "host_email": "", "accounts": 0}
    return {
        "connected": True,
        "host_email": acc.host_email or "(no email)",
        "account_id": _mask(acc.account_id),
        "accounts": 1,
    }


def _telegram_status() -> dict:
    with SyncSession() as s:
        chs = s.query(TelegramChannel).filter_by(
            tenant_id="default", enabled=True
        ).count()
    bot_token = settings.telegram_bot_token or ""
    return {
        "enabled": chs > 0,
        "channels": chs,
        "bot": _mask(bot_token) if bot_token else "(未配置)",
    }
