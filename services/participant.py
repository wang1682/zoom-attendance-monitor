"""
ParticipantService — Zoom 参与者事件写入

统一处理：
- zoom_participants INSERT（enter / leave / breakout_enter / breakout_leave）
- sharing_live INSERT + UPDATE（sharing_started → insert, sharing_ended → close）
- seen_emails 新人检测
- 事件去重 + 租户隔离

设计原则：
- 所有写入方法接收显式 tenant_id 参数
- 去重在 Service 层做（业务去重），不在 db.py 做
- sharing_live 的 opened/closed 逻辑完全收拢在此
"""

import json
import re
import sys
from datetime import datetime, timezone, timedelta

import db as _db

# ── 去重 ──
_PARTICIPANT_DEDUP: set[str] = set()
_PARTICIPANT_DEDUP_TTL = 120  # seconds


def _pkey(tenant_id: str, meeting_id: str, name: str, action: str, action_time: str) -> str:
    return f"{tenant_id}|{meeting_id}|{name}|{action}|{action_time}"


def _clean_dedup():
    """清理过期去重记录（惰性，只在此函数被调用时清理）"""
    # 当集合太大时清理 — 简单实现：超过 10000 条就全清
    global _PARTICIPANT_DEDUP
    if len(_PARTICIPANT_DEDUP) > 10000:
        _PARTICIPANT_DEDUP.clear()


class ParticipantService:
    """参与者事件写入服务"""

    # ------------------------------------------------------------------
    # zoom_participants 写入
    # ------------------------------------------------------------------

    @staticmethod
    def save_participant(
        meeting_id: str,
        name: str,
        email: str,
        action: str,
        action_time: datetime,
        tenant_id: str = "unknown",
        source: str = "poll",
    ) -> int | None:
        """
        保存参与者进出记录。

        支持的 action 值：
        - "enter"/"leave" — 常规进出
        - "breakout_enter"/"breakout_leave" — 分组讨论进出
        - "waiting_room_enter" — 等候室加入
        - "admitted" — 被准入
        - "unknown" — 未知行为

        Returns:
            int: 记录 id
            None: 被去重跳过
        """
        action_time_str = action_time.isoformat()

        # 去重
        key = _pkey(tenant_id, meeting_id, name, action, action_time_str)
        _clean_dedup()
        if key in _PARTICIPANT_DEDUP:
            sys.stdout.write(
                f"[ParticipantService] 跳过重复记录: {action} "
                f"{name} @ {meeting_id} tenant={tenant_id}\n"
            )
            sys.stdout.flush()
            return None
        _PARTICIPANT_DEDUP.add(key)

        return _db.save_participant(
            meeting_id, name, email,
            action, action_time,
            source=source, tenant_id=tenant_id,
        )
        # 同步写入 participant_sessions
        try:
            _session_id = _db.save_participant_session(
                meeting_id, name, action, action_time,
                tenant_id=tenant_id, source=source,
            )
            # shadow mode 对比日志（仅 enter/leave 动作触发）
            if _session_id is not None and action in ("enter", "joined", "leave", "left"):
                _log_session_comparison(tenant_id, name)
        except Exception:
            # participant_sessions 是辅助，不影响主流程
            pass

        return result

    @staticmethod
    def save_webhook_participant(
        event_type: str,
        participant: dict,
        meeting_id: str,
        tenant_id: str = "unknown",
    ) -> int | None:
        """
        从 webhook payload 提取参与者信息并写入。

        自动解析：
        - participant_joined → action="enter"
        - participant_left → action="leave"
        - breakout_joined → action="breakout_enter"
        - breakout_left → action="breakout_leave"
        - waiting_room_joined → action="waiting_room_enter"
        - participant_admitted → action="admitted"
        """
        name = participant.get("user_name", "").strip()
        email = participant.get("email", "")

        # 确定 action
        raw = event_type.lower()
        if "breakout" in raw and ("joined" in raw or "enter" in raw):
            action = "breakout_enter"
        elif "breakout" in raw and ("left" in raw or "leave" in raw):
            action = "breakout_leave"
        elif "waiting_room" in raw and ("joined" in raw or "enter" in raw):
            action = "waiting_room_enter"
        elif "admitted" in raw:
            action = "admitted"
        elif "joined" in raw:
            action = "enter"
        elif "left" in raw:
            action = "leave"
        else:
            action = "unknown"

        # 解析时间
        raw_time = (
            participant.get("join_time")
            or participant.get("leave_time")
            or participant.get("date_time")
            or ""
        )
        if raw_time:
            try:
                action_time = datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            except Exception:
                action_time = datetime.now(timezone.utc)
        else:
            action_time = datetime.now(timezone.utc)

        return ParticipantService.save_participant(
            meeting_id, name, email,
            action, action_time,
            tenant_id=tenant_id,
            source="webhook",
        )

    # ------------------------------------------------------------------
    # sharing_live 写入/更新
    # ------------------------------------------------------------------

    @staticmethod
    def save_sharing_started(
        meeting_id: str,
        user_name: str,
        user_id: str,
        content: str,
        start_time: str,
        tenant_id: str = "unknown",
    ) -> int | None:
        """
        记录 sharing_started 事件。

        逻辑：
        1. 先关闭同租户+同 meeting+同 user_name 的旧 is_active 记录
        2. 如果已经有 active 记录，update start_time 而非 INSERT（防重复）
        3. 否则插入新记录
        """
        conn = _db._get_conn()
        now_iso = datetime.now(timezone.utc).isoformat()

        if tenant_id:
            # 先检查是否已有 active 记录 → 防重复
            existing = conn.execute(
                "SELECT id FROM sharing_live "
                "WHERE meeting_id=? AND user_name=? AND is_active=1 AND tenant_id=?",
                (meeting_id, user_name, tenant_id),
            ).fetchone()

            if existing:
                # 已有记录 → 更新 start_time，不 INSERT
                conn.execute(
                    "UPDATE sharing_live SET start_time=?, content=?, user_id=?, updated_at=? "
                    "WHERE id=? AND tenant_id=?",
                    (start_time, content, user_id, now_iso, existing[0], tenant_id),
                )
                conn.commit()
                return existing[0]

            # 先关闭同 meeting+同 user_name 的旧记录（保险）
            conn.execute(
                "UPDATE sharing_live SET is_active=0, end_time=?, updated_at=? "
                "WHERE meeting_id=? AND user_name=? AND is_active=1 AND tenant_id=?",
                (start_time, now_iso, meeting_id, user_name, tenant_id),
            )

        cur = conn.execute(
            "INSERT INTO sharing_live "
            "(meeting_id, user_name, user_id, content, start_time, "
            " is_active, source, created_at, updated_at, tenant_id) "
            "VALUES (?, ?, ?, ?, ?, 1, 'webhook', ?, ?, ?)",
            (meeting_id, user_name, user_id, content, start_time,
             now_iso, now_iso, tenant_id or "unknown"),
        )
        conn.commit()
        return cur.lastrowid

    @staticmethod
    def save_sharing_ended(
        meeting_id: str,
        user_name: str,
        user_id: str,
        end_time: str,
        tenant_id: str = "unknown",
    ) -> bool:
        """
        记录 sharing_ended 事件。

        逻辑：
        1. 优先按 meeting_id + user_name + tenant_id 匹配 active 记录
        2. 如果没找到，返回 False（无 active session 被关闭）

        Returns:
            True: 有 active session 被关闭
            False: 无匹配 active session（残留/过期 sharing_ended）
        """
        conn = _db._get_conn()
        now_iso = datetime.now(timezone.utc).isoformat()

        affected = conn.execute(
            "UPDATE sharing_live SET end_time=?, is_active=0, updated_at=? "
            "WHERE meeting_id=? AND user_name=? AND is_active=1 AND tenant_id=?",
            (end_time, now_iso, meeting_id, user_name, tenant_id or "unknown"),
        ).rowcount

        conn.commit()
        return affected > 0

    @staticmethod
    def save_webhook_sharing(
        event_type: str,
        participant: dict,
        meeting_id: str,
        tenant_id: str = "unknown",
    ) -> int | bool | None:
        """
        从 webhook payload 处理 sharing 事件。

        自动判断 sharing_started → INSERT 或 sharing_ended → UPDATE。
        """
        user_name = participant.get("user_name", "").strip()
        raw_uid = str(participant.get("user_id", ""))

        # Breakout room 的 user_id 可能被附加时间戳
        if re.search(r"20\d{2}-\d{2}-\d{2}", raw_uid):
            m = re.match(r"^(\d+)", raw_uid)
            user_id = m.group(1) if m else ""
        else:
            user_id = re.sub(r"[^0-9]", "", raw_uid)[:20]

        sd = participant.get("sharing_details", {})
        content = sd.get("content", "")
        dt_str = sd.get("date_time", "")

        raw = event_type.lower()
        if "sharing_started" in raw:
            return ParticipantService.save_sharing_started(
                meeting_id, user_name, user_id, content, dt_str, tenant_id,
            )
        elif "sharing_ended" in raw:
            return ParticipantService.save_sharing_ended(
                meeting_id, user_name, user_id, dt_str, tenant_id,
            )

        return None

    # ------------------------------------------------------------------
    # seen_emails — 新人检测
    # ------------------------------------------------------------------

    @staticmethod
    def check_new_participant(
        email: str,
        name: str,
        now: datetime | None = None,
    ) -> bool:
        """
        检测是否为新参与者。
        注意：此方法暂不携带 tenant_id（db.check_new_email 也未使用 tenant_id）。
        保留给后续迁移。
        """
        if now is None:
            now = datetime.now(timezone.utc)
        return _db.check_new_email(email, name, now)

    # ------------------------------------------------------------------
    # 批量操作辅助
    # ------------------------------------------------------------------

    @staticmethod
    def save_poll_participants(
        tenant_id: str,
        enters: list[tuple[str, datetime, str, str]],
        leaves: list[tuple[str, datetime, str]],
    ) -> tuple[list, list]:
        """
        批量处理 monitor poll 的进出记录。

        enters: [(name, datetime, meeting_id, email), ...]
        leaves: [(name, datetime, meeting_id), ...]

        Returns:
            (saved_enters, saved_leaves) — 各自返回实际写入的记录
        """
        saved_enters = []
        for name, utc_dt, mid, email in enters:
            pid = ParticipantService.save_participant(
                mid, name, email, "enter", utc_dt,
                tenant_id=tenant_id, source="poll",
            )
            if pid is not None:
                saved_enters.append((name, utc_dt, mid, email))

        saved_leaves = []
        for name, utc_dt, mid in leaves:
            pid = ParticipantService.save_participant(
                mid, name, "", "leave", utc_dt,
                tenant_id=tenant_id, source="poll",
            )
            if pid is not None:
                saved_leaves.append((name, utc_dt, mid))

        return saved_enters, saved_leaves

    @staticmethod
    def create_stranger_alert(
        name: str,
        email: str,
        utc_dt: datetime,
        mid: str,
    ) -> int | None:
        """创建陌生人告警（纯数据写入，不负责推送）"""
        if not email:
            return None
        return _db.create_alert(
            alert_type="stranger",
            title=f"陌生来访: {name}",
            message=f"邮箱 {email} 首次出现",
            severity="warning",
            related_name=name,
            related_email=email,
        )


# ──────────────────────────────────────────
# shadow mode 对比日志
# ──────────────────────────────────────────

def _get_session_summary(tenant_id: str, name: str) -> dict | None:
    """从 participant_sessions 获取某人的今日累计"""
    try:
        user_key = _db._make_user_key(name)
        rows = _db._get_conn().execute("""
            SELECT
                COALESCE(SUM(duration_seconds), 0) +
                CASE WHEN MAX(CASE WHEN leave_time_utc IS NULL THEN join_time_utc END) IS NOT NULL
                    THEN CAST((JULIANDAY('now') - JULIANDAY(MAX(CASE WHEN leave_time_utc IS NULL THEN join_time_utc END))) * 86400 AS INTEGER)
                    ELSE 0
                END AS total_seconds,
                MAX(CASE WHEN leave_time_utc IS NULL THEN 1 ELSE 0 END) AS is_online
            FROM participant_sessions
            WHERE tenant_id=? AND user_key=?
        """, (tenant_id, user_key)).fetchone()
        if rows:
            return {"total_seconds": rows[0], "is_online": bool(rows[1])}
    except Exception:
        pass
    return None


def _log_session_comparison(tenant_id: str, name: str):
    """对比旧算法 vs session 算法并输出到日志"""
    from io import StringIO
    import sys
    try:
        # 旧算法
        old = _db.get_today_attendance_summary(tenant_id=tenant_id)
        old_display = _db.resolve_display_name(name, tenant_id)["display_name"]
        old_row = old.get(old_display)
        old_total = old_row["today_total_seconds"] if old_row else 0
        old_online = old_row["status"] == "online" if old_row else False

        # 新算法
        new = _get_session_summary(tenant_id, name)
        if new is None:
            return
        new_total = new["total_seconds"]
        new_online = new["is_online"]

        diff = new_total - old_total
        buf = StringIO()
        buf.write(f"[SESSION_COMPARE] {name:<20s} tenant={tenant_id}")
        buf.write(f" | old={old_total:>6d}s online={old_online}")
        buf.write(f" | session={new_total:>6d}s online={new_online}")
        buf.write(f" | diff={diff:>+6d}s")
        if abs(diff) > 60:
            buf.write(" *** LARGE DIFF ***")
        print(buf.getvalue(), file=sys.stderr)
    except Exception:
        pass
