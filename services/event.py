"""
EventService — Zoom Webhook 事件持久化记录

负责：
- zoom_events 表的插入（按租户隔离，带基础去重）
- 事件类型规范化映射
- 作为其他 Service 的事件日志底层依赖

设计原则：
- 纯数据层，不涉及业务判断（不检查是否要推送）
- 去重策略：同 event_type + 同一 payload 的 id 在短时间内不重复写
"""

import hashlib
import json
import sys
from datetime import datetime, timezone, timedelta

import db as _db

# ── 去重: 临时 bloom-like 集合 ──
# key: sha256(event_type + payload_json)
# TTL: 60 秒 — 防重复 webhook 双发
_EVENT_DEDUP: dict[str, float] = {}
_EVENT_DEDUP_TTL = 60.0


def _event_dedup_key(event_type: str, payload: dict) -> str:
    """生成事件去重 key"""
    raw = event_type + json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _clean_expired_dedup():
    """清理过期去重记录"""
    now = datetime.now(timezone.utc).timestamp()
    stale = [k for k, t in _EVENT_DEDUP.items() if now - t > _EVENT_DEDUP_TTL]
    for k in stale:
        _EVENT_DEDUP.pop(k, None)


# ── 事件类型映射 ──
# webhook 原始 event → 规范化的 event_type（用于 zoom_events.payload 标签）
EVENT_TYPE_LABELS = {
    "meeting.participant_joined": "participant_joined",
    "meeting.participant_left": "participant_left",
    "meeting.participant_joined_breakout_room": "breakout_room_joined",
    "meeting.participant_left_breakout_room": "breakout_room_left",
    "meeting.sharing_started": "sharing_started",
    "meeting.sharing_ended": "sharing_ended",
    "meeting.breakout_room_sharing_started": "sharing_started",
    "meeting.breakout_room_sharing_ended": "sharing_ended",
    "meeting.participant_joined_waiting_room": "waiting_room_joined",
    "meeting.participant_admitted": "participant_admitted",
    "meeting.unknown_user": "unknown_user",
}

# 硬阻断预警推送的事件（只记录，不触发规则）
HARD_BLOCKED_EVENTS = frozenset({
    "breakout_room_joined",
    "breakout_room_left",
})


def normalize_event_type(raw_event: str) -> str:
    """将 Zoom webhook 原始 event 转为规范化类型"""
    return EVENT_TYPE_LABELS.get(raw_event, raw_event)


class EventService:
    """Zoom Webhook 事件记录服务"""

    @staticmethod
    def save_event(
        event_type: str,
        payload: dict,
        tenant_id: str = "unknown",
        dedup: bool = True,
    ) -> int | None:
        """
        记录 webhook 事件到 zoom_events 表。

        Returns:
            int: 新插入记录的 id
            None: 被去重跳过
        """
        # 去重检查
        if dedup:
            _clean_expired_dedup()
            dk = _event_dedup_key(event_type, payload)
            if dk in _EVENT_DEDUP:
                sys.stdout.write(
                    f"[EventService] 跳过重复事件: {event_type} "
                    f"tenant={tenant_id}\n"
                )
                sys.stdout.flush()
                return None
            _EVENT_DEDUP[dk] = datetime.now(timezone.utc).timestamp()

        event_id = _db.save_webhook_event(event_type, payload, tenant_id=tenant_id)
        return event_id

    @staticmethod
    def save_raw_event(payload: dict, tenant_id: str = "unknown") -> int | None:
        """
        记录原始 webhook payload（不解析事件类型）。
        用于 Zoom challenge / 无法识别的事件。
        """
        event_type = payload.get("event", "unknown")
        return EventService.save_event(event_type, payload, tenant_id)

    @staticmethod
    def log_event(
        event_type: str,
        payload: dict,
        tenant_id: str = "unknown",
        skip_dedup: bool = False,
    ) -> int:
        """
        简化的快速记录方法。
        区别于 save_event: 返回 int（一直插入，无 None 风险），默认不去重。

        用于 monitor.py 等非 webhook 路径记录。
        """
        return _db.save_webhook_event(event_type, payload, tenant_id=tenant_id)

    @staticmethod
    def get_recent_events(
        tenant_id: str,
        limit: int = 50,
        event_type: str | None = None,
    ) -> list[dict]:
        """获取最近的事件记录（用于调试/展示）"""
        conn = _db._get_conn()
        if event_type:
            rows = conn.execute(
                "SELECT id, event_type, payload, created_at FROM zoom_events "
                "WHERE tenant_id=? AND event_type=? ORDER BY id DESC LIMIT ?",
                (tenant_id, event_type, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, event_type, payload, created_at FROM zoom_events "
                "WHERE tenant_id=? ORDER BY id DESC LIMIT ?",
                (tenant_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]
