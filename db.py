"""db.py — SQLite 数据库操作
Schema:
  - zoom_events:     原始 Webhook 事件
  - zoom_participants:参会记录（进出）
  - seen_emails:     邮箱去重
  - alerts:          告警日志
  - settings:        持久化设置（命令控制）
  - telegram_alert_rules: Telegram 告警规则（替代旧的 alert_rules）
  - audit_logs:      操作审计日志
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path

from config import settings

MYT = timezone(timedelta(hours=8))

def to_myt_str(dt_str: str) -> str:
    """将 UTC ISO/Datetime 字符串转 MYT MM-DD HH:mm:ss"""
    if not dt_str:
        return ""
    try:
        s = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MYT).strftime("%m-%d %H:%M:%S")
    except Exception:
        return dt_str[:16]

DB_PATH = settings.database_url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
_local = threading.local()


def _get_conn() -> sqlite3.Connection:
    """线程级单例连接（带存活检查）"""
    readonly = os.environ.get("DB_READONLY") == "true"
    if not hasattr(_local, "conn") or _local.conn is None:
        if readonly:
            _local.conn = sqlite3.connect("file:" + DB_PATH + "?mode=ro", uri=True)
        else:
            _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _local.conn.row_factory = sqlite3.Row
        return _local.conn
    try:
        _local.conn.execute("SELECT 1").fetchone()
    except sqlite3.ProgrammingError:
        if readonly:
            _local.conn = sqlite3.connect("file:" + DB_PATH + "?mode=ro", uri=True)
        else:
            _local.conn = sqlite3.connect(DB_PATH)
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA busy_timeout=5000")
        _local.conn.row_factory = sqlite3.Row
    return _local.conn


def init_db(readonly: bool = False):
    """建表（幂等，并发安全 — 逐语句执行 + WAL busy_timeout）"""
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = _get_conn()
    statements = [
        "CREATE TABLE IF NOT EXISTS zoom_events ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  event_type TEXT NOT NULL,"
        "  payload TEXT NOT NULL,"
        "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")",

        "CREATE TABLE IF NOT EXISTS zoom_participants ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  meeting_id TEXT NOT NULL,"
        "  name TEXT NOT NULL,"
        "  email TEXT NOT NULL DEFAULT '',"
        "  action TEXT NOT NULL,"
        "  action_time TEXT NOT NULL,"
        "  source TEXT NOT NULL DEFAULT 'poll',"
        "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")",

        "CREATE TABLE IF NOT EXISTS seen_emails ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  email TEXT NOT NULL UNIQUE,"
        "  name TEXT NOT NULL DEFAULT '',"
        "  first_seen TEXT NOT NULL,"
        "  last_seen TEXT NOT NULL,"
        "  seen_count INTEGER NOT NULL DEFAULT 1"
        ")",

        "CREATE TABLE IF NOT EXISTS alerts ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  alert_type TEXT NOT NULL,"
        "  severity TEXT NOT NULL DEFAULT 'info',"
        "  title TEXT NOT NULL,"
        "  message TEXT NOT NULL DEFAULT '',"
        "  related_name TEXT NOT NULL DEFAULT '',"
        "  related_email TEXT NOT NULL DEFAULT '',"
        "  sent_to TEXT NOT NULL DEFAULT '',"
        "  success INTEGER NOT NULL DEFAULT 0,"
        "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")",

        "CREATE TABLE IF NOT EXISTS settings ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  key TEXT NOT NULL UNIQUE,"
        "  value TEXT NOT NULL DEFAULT ''"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_participants_meeting ON zoom_participants(meeting_id)",
        "CREATE INDEX IF NOT EXISTS idx_participants_time   ON zoom_participants(action_time)",
        "CREATE INDEX IF NOT EXISTS idx_events_type          ON zoom_events(event_type)",
        "CREATE INDEX IF NOT EXISTS idx_events_created       ON zoom_events(created_at)",
        "CREATE TABLE IF NOT EXISTS meeting_topics ("
        "  meeting_id TEXT PRIMARY KEY,"
        "  topic TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT"
        ")",

        "CREATE TABLE IF NOT EXISTS meeting_topics ("
        "  meeting_id TEXT PRIMARY KEY,"
        "  topic TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_alerts_type          ON alerts(alert_type)",
        "CREATE INDEX IF NOT EXISTS idx_alerts_created       ON alerts(created_at)",
        "CREATE TABLE IF NOT EXISTS member_aliases ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  canonical_name TEXT NOT NULL,"
        "  alias_name TEXT NOT NULL UNIQUE,"
        "  count_enabled INTEGER NOT NULL DEFAULT 1,"
        "  note TEXT DEFAULT '',"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")",
        
        "CREATE INDEX IF NOT EXISTS idx_aliases_canonical ON member_aliases(canonical_name)",
        "CREATE INDEX IF NOT EXISTS idx_aliases_alias     ON member_aliases(alias_name)",
        "CREATE TABLE IF NOT EXISTS member_display ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  raw_name TEXT NOT NULL UNIQUE,"
        "  display_name TEXT NOT NULL,"
        "  match_key TEXT NOT NULL DEFAULT '',"
        "  count_enabled INTEGER NOT NULL DEFAULT 1,"
        "  note TEXT DEFAULT '',"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_md_raw    ON member_display(raw_name)",
        "CREATE INDEX IF NOT EXISTS idx_md_match  ON member_display(match_key)",
        "CREATE INDEX IF NOT EXISTS idx_md_display ON member_display(display_name)",

        "CREATE TABLE IF NOT EXISTS sharing_live ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  meeting_id TEXT NOT NULL DEFAULT '',"
        "  user_name TEXT NOT NULL,"
        "  user_id TEXT NOT NULL DEFAULT '',"
        "  content TEXT DEFAULT '',"
        "  start_time TEXT,"
        "  end_time TEXT,"
        "  is_active INTEGER NOT NULL DEFAULT 1,"
        "  source TEXT DEFAULT 'webhook',"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")",
        
        "CREATE INDEX IF NOT EXISTS idx_sharing_active ON sharing_live(is_active)",
        "CREATE INDEX IF NOT EXISTS idx_sharing_user   ON sharing_live(user_name)",
        "CREATE TABLE IF NOT EXISTS alert_rules ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  rule_type TEXT NOT NULL UNIQUE,"
        "  enabled INTEGER NOT NULL DEFAULT 1,"
        "  threshold_minutes INTEGER DEFAULT 30,"
        "  threshold_count INTEGER DEFAULT 10,"
        "  chat_id TEXT DEFAULT '',"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_alert_rules_type ON alert_rules(rule_type)",

        "CREATE TABLE IF NOT EXISTS alert_sent ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  alert_key TEXT NOT NULL UNIQUE,"
        "  rule_type TEXT NOT NULL,"
        "  sent_at TEXT"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_alert_sent_key ON alert_sent(alert_key)",

        "CREATE TABLE IF NOT EXISTS telegram_alert_rules ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  event_type TEXT NOT NULL UNIQUE,"
        "  title TEXT NOT NULL DEFAULT '',"
        "  enabled INTEGER NOT NULL DEFAULT 1,"
        "  target_chat_id TEXT DEFAULT '',"
        "  target_channel_id INTEGER DEFAULT NULL,"
        "  cooldown_seconds INTEGER DEFAULT 0,"
        "  quiet_enabled INTEGER DEFAULT 0,"
        "  quiet_start TEXT DEFAULT '00:00',"
        "  quiet_end TEXT DEFAULT '08:00',"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_telegram_rules_event ON telegram_alert_rules(event_type)",

        "CREATE TABLE IF NOT EXISTS telegram_channels ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name TEXT NOT NULL,"
        "  chat_id TEXT NOT NULL,"
        "  enabled INTEGER DEFAULT 1,"
        "  is_default INTEGER DEFAULT 0,"
        "  notes TEXT DEFAULT '',"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_telegram_channels_chat ON telegram_channels(chat_id)",


        "CREATE TABLE IF NOT EXISTS member_groups ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  name TEXT NOT NULL UNIQUE,"
        "  description TEXT DEFAULT '',"
        "  created_at TEXT,"
        "  updated_at TEXT"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_member_groups_name ON member_groups(name)",

        "CREATE TABLE IF NOT EXISTS member_group_members ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  group_id INTEGER NOT NULL REFERENCES member_groups(id),"
        "  member_name TEXT NOT NULL,"
        "  created_at TEXT,"
        "  UNIQUE(group_id, member_name)"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_mgm_group ON member_group_members(group_id)",
        "CREATE INDEX IF NOT EXISTS idx_mgm_member ON member_group_members(member_name)",
        "CREATE TABLE IF NOT EXISTS audit_logs ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  action TEXT NOT NULL,"
        "  entity_type TEXT NOT NULL DEFAULT 'telegram_alert_rule',"
        "  entity_id INTEGER,"
        "  details TEXT DEFAULT '',"
        "  created_at TEXT"
        ")",

    ]
    for sql in statements:
        try:
            conn.execute(sql)
        except sqlite3.OperationalError as e:
            # 并发场景下 CREATE INDEX 可能因列未就绪失败，重试一次
            if "no such column" in str(e):
                conn.execute(sql)
            else:
                raise
    conn.commit()

    # migrate: add target_channel_id column if not exists (SQLite compat)
    try:
        conn.execute("ALTER TABLE telegram_alert_rules ADD COLUMN target_channel_id INTEGER DEFAULT NULL")
    except Exception:
        pass

    # migrate: add group_id column to member_display
    try:
        conn.execute("ALTER TABLE member_display ADD COLUMN group_id INTEGER DEFAULT NULL")
    except Exception:
        pass

    # migrate: move member_group_members data to member_display.group_id
    try:
        rows = conn.execute(
            "SELECT mgm.group_id, mgm.member_name FROM member_group_members mgm "
            "LEFT JOIN member_display md ON md.raw_name = mgm.member_name "
            "WHERE md.id IS NOT NULL"
        ).fetchall()
        for gid, mname in rows:
            conn.execute(
                "UPDATE member_display SET group_id = ? WHERE raw_name = ? AND (group_id IS NULL OR group_id != ?)",
                (gid, mname, gid),
            )
        # For members not yet in member_display, create placeholder entries
        rows2 = conn.execute(
            "SELECT mgm.group_id, mgm.member_name FROM member_group_members mgm "
            "LEFT JOIN member_display md ON md.raw_name = mgm.member_name "
            "WHERE md.id IS NULL"
        ).fetchall()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for gid, mname in rows2:
            match_key = mname.strip().lower().replace(" ", "")
            conn.execute(
                "INSERT OR IGNORE INTO member_display (raw_name, display_name, match_key, group_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                (mname, mname, match_key, gid, now, now),
            )
        conn.commit()
    except Exception:
        pass

    # migrate: add zoom_accounts columns for account config page
    for col_sql in [
        "ALTER TABLE zoom_accounts ADD COLUMN webhook_secret TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE zoom_accounts ADD COLUMN status TEXT NOT NULL DEFAULT 'inactive'",
        "ALTER TABLE zoom_accounts ADD COLUMN last_sync TEXT",
        "ALTER TABLE zoom_accounts ADD COLUMN last_sync_result TEXT",
        "ALTER TABLE zoom_accounts ADD COLUMN webhook_last_event TEXT",
        "ALTER TABLE zoom_accounts ADD COLUMN webhook_last_time TEXT",
        "ALTER TABLE zoom_accounts ADD COLUMN updated_at TEXT",
    ]:
        try:
            conn.execute(col_sql)
        except Exception:
            pass

    # migrate: add tenant_id to users table for simplified role model
    try:
        conn.execute("ALTER TABLE users ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
    except Exception:
        pass

    conn.commit()

    # seed default telegram alert rules
    if not readonly: seed_telegram_rules()
    if not readonly: seed_member_groups()

    # seed default telegram channel
    _seed_default_telegram_channel()

    # multi-tenant migrations
    if not readonly:
        run_mt_migrations()


# ── zoom_events ──────────────────────────────────────────────────────────────

def save_webhook_event(event_type: str, payload: dict, tenant_id: str = "unknown") -> int:
    import json
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO zoom_events (event_type, payload, tenant_id) VALUES (?, ?, ?)",
        (event_type, json.dumps(payload, ensure_ascii=False), tenant_id),
    )
    conn.commit()
    return cur.lastrowid


def get_recent_events(limit: int = 50, tenant_id: str = None) -> list[dict]:
    conn = _get_conn()
    if tenant_id:
        rows = conn.execute(
            "SELECT * FROM zoom_events WHERE tenant_id = ? ORDER BY id DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM zoom_events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        # 从 payload 提取 user_name 和 event_time
        try:
            top = json.loads(d["payload"])
            # 兼容两种结构：直接 {payload:{...}} 或 {object:{...}}
            payload = top.get("payload", top)
            obj = payload.get("object", {})
            part = obj.get("participant", {})
            user_name = part.get("user_name") or obj.get("user_name", "")
            if not user_name:
                user_name = part.get("participant_user_name", "")
            d["user_name"] = user_name
            join_time = part.get("join_time") or ""
            if join_time:
                d["event_time"] = join_time
            else:
                d["event_time"] = d.get("created_at", "")
        except:
            d["user_name"] = ""
            d["event_time"] = d.get("created_at", "")
        # 将 UTC 时间转成 MYT 显示
        d["event_time"] = to_myt_str(d.get("event_time", ""))
        d["created_at"] = to_myt_str(d.get("created_at", ""))
        result.append(d)
    return result


# ── zoom_participants ────────────────────────────────────────────────────────


def get_events_paginated(
    tenant_id: str,
    page: int = 1,
    per_page: int = 50,
    search: str = "",
    event_type: str = "",
) -> tuple[list[dict], int, int]:
    """返回 (events_list, total_count, total_pages) — 分页+搜索+类型筛选，强制 tenant_id 过滤。"""
    conn = _get_conn()
    wheres = ["tenant_id = ?"]
    params: list = [tenant_id]

    if search:
        wheres.append("(event_type LIKE ? OR payload LIKE ?)")
        like = f"%{search}%"
        params.extend([like, like])

    if event_type:
        wheres.append("event_type = ?")
        params.append(event_type)

    where_sql = " AND ".join(wheres)
    row = conn.execute(
        f"SELECT COUNT(*) AS c FROM zoom_events WHERE {where_sql}", params
    ).fetchone()
    total = row["c"] if row else 0
    total_pages = max(1, (total + per_page - 1) // per_page)

    offset = (page - 1) * per_page
    rows = conn.execute(
        f"SELECT * FROM zoom_events WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        [*params, per_page, offset],
    ).fetchall()
    events = [dict(r) for r in rows]

    return events, total, total_pages


def get_distinct_event_types(tenant_id: str) -> list[str]:
    """获取该租户有数据的事件类型列表（用于下拉筛选）。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT DISTINCT event_type FROM zoom_events WHERE tenant_id = ? AND event_type IS NOT NULL AND event_type != '' ORDER BY event_type",
        (tenant_id,),
    ).fetchall()
    return [r["event_type"] for r in rows]


# ── zoom_participants ────────────────────────────────────────────────────────


def save_participant(
    meeting_id: str, name: str, email: str,
    action: str, action_time: datetime,
    source: str = "poll",
    tenant_id: str = "unknown",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO zoom_participants (meeting_id, name, email, action, action_time, source, tenant_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (meeting_id, name, email, action, action_time.isoformat(), source, tenant_id),
    )
    conn.commit()
    return cur.lastrowid


def get_today_participants(limit: int = 200, tenant_id: str = None) -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _get_conn()
    if tenant_id:
        rows = conn.execute(
            "SELECT * FROM zoom_participants WHERE action_time >= ? AND tenant_id = ? ORDER BY action_time DESC LIMIT ?",
            (today, tenant_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM zoom_participants WHERE action_time >= ? ORDER BY action_time DESC LIMIT ?",
            (today, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def _fmt_dur(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m"
    hours = minutes // 60
    mins = minutes % 60
    return f"{hours}h{mins}m" if mins else f"{hours}h"


def _myt_short(utc_str: str) -> str:
    """UTC → MYT MM-DD HH:mm"""
    if not utc_str:
        return ""
    from datetime import datetime, timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    try:
        s = utc_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(MYT).strftime("%m-%d %H:%M")
    except:
        return utc_str[:5]


def get_today_attendance_summary(tenant_id: str = None) -> dict:
    """今日参会汇总 — 每人一行，聚合 Join/Leave 事件
    
    用 resolve_display_name 标准化名字，计算累计时长、进出次数、当前状态。
    不修改数据库，不做 schema 变更。
    """
    from datetime import datetime, timezone, timedelta
    from collections import OrderedDict

    now_utc = datetime.now(timezone.utc)
    now_myt = now_utc + timedelta(hours=8)
    today_start_myt = now_myt.replace(hour=0, minute=0, second=0, microsecond=0)
    today_start_utc = today_start_myt - timedelta(hours=8)
    # 往前多查 6 小时，覆盖 MYT 00:00 前就已在线的参会者
    query_start_utc = today_start_utc - timedelta(hours=6)
    today_utc_str = query_start_utc.strftime("%Y-%m-%dT%H:%M:%S")

    if tenant_id:
        rows = _get_conn().execute(
            "SELECT * FROM zoom_participants WHERE action_time >= ? AND tenant_id = ? ORDER BY name, action_time",
            (today_utc_str, tenant_id),
        ).fetchall()
    else:
        rows = _get_conn().execute(
            "SELECT * FROM zoom_participants WHERE action_time >= ? ORDER BY name, action_time",
            (today_utc_str,),
        ).fetchall()
    raw = [dict(r) for r in rows]

    # ── 按 resolve_display_name 分组 ──
    members = OrderedDict()
    for e in raw:
        resolved = resolve_display_name(e["name"])
        display_name = resolved["display_name"]

        if display_name not in members:
            members[display_name] = {
                "standard_name": display_name,
                "group_name": get_member_group(display_name, tenant_id) or "",
                "status": "offline",
                "first_join": None,
                "today_total_seconds": 0,
                "today_total_duration": "0m",
                "join_count": 0,
                "leave_count": 0,
                "last_activity": None,
                "last_action": None,
                "email": "",
                "raw_events": [],
            }

        m = members[display_name]
        action, at = e["action"], e["action_time"]

        if action in ("enter", "joined"):
            m["join_count"] += 1
            if m["first_join"] is None or at < m["first_join"]:
                m["first_join"] = at
            m["last_action"] = "enter"
        elif action in ("leave", "left"):
            m["leave_count"] += 1
            m["last_action"] = "leave"

        # ── 更新 email：用今天数据里最新一条（按 action_time） ──
        m["raw_events"].append({"action": action, "action_time": at, "meeting_id": e["meeting_id"], "email": e.get("email", "")})
        m["last_activity"] = at

    # ── 计算时长 & 状态 ──
    for m in members.values():
        m["raw_events"].sort(key=lambda x: x["action_time"])
        deduped = []
        for ev in m["raw_events"]:
            if deduped and deduped[-1]["action"] in ("enter", "joined", "leave", "left") \
               and deduped[-1]["action"] == ev["action"]:
                continue
            deduped.append(ev)

        total_seconds = 0
        i = 0
        while i < len(deduped):
            ev = deduped[i]
            if ev["action"] in ("enter", "joined"):
                enter_dt = datetime.fromisoformat(ev["action_time"])
                # 只算今日 MYT 范围内的时长
                if enter_dt < today_start_utc:
                    enter_dt = today_start_utc
                leave_dt = None
                for j in range(i + 1, len(deduped)):
                    if deduped[j]["action"] in ("leave", "left"):
                        leave_dt = datetime.fromisoformat(deduped[j]["action_time"])
                        i = j
                        break
                end_dt = leave_dt or now_utc
                dur = (end_dt - enter_dt).total_seconds()
                if 0 < dur < 86400:
                    total_seconds += dur
            i += 1

        m["today_total_seconds"] = int(total_seconds)
        m["today_total_duration"] = _fmt_dur(int(total_seconds))
        # ── 状态判断：最后动作是 enter 且最近 activity 在 15 分钟内才算在线 ──
        if m["last_action"] == "enter" and m["last_activity"]:
            try:
                last_dt = datetime.fromisoformat(m["last_activity"])
                is_online = (now_utc - last_dt).total_seconds() < 900
            except Exception:
                is_online = False
        else:
            is_online = False
        m["status"] = "online" if is_online else "offline"

        # ── 策略C：从今天 raw_events 取最新一条有 email 的记录 ──
        for ev in reversed(m["raw_events"]):
            if ev.get("email"):
                m["email"] = ev["email"]
                break
        # ── 今天没有 email，查全局历史最近 ──
        if not m["email"]:
            if tenant_id:
                row = _get_conn().execute(
                    "SELECT email FROM zoom_participants WHERE LOWER(name) = LOWER(?) AND email IS NOT NULL AND email != '' AND tenant_id=? ORDER BY action_time DESC LIMIT 1",
                    (m["standard_name"], tenant_id),
                ).fetchone()
            else:
                row = _get_conn().execute(
                    "SELECT email FROM zoom_participants WHERE LOWER(name) = LOWER(?) AND email IS NOT NULL AND email != '' ORDER BY action_time DESC LIMIT 1",
                    (m["standard_name"],),
                ).fetchone()
            if row:
                m["email"] = row[0]

    # ── 排序：在线优先 → 时长降序 ──
    sorted_members = sorted(
        members.values(),
        key=lambda m: (0 if m["status"] == "online" else 1, -(m["today_total_seconds"] or 0)),
    )

    for m in sorted_members:
        m.pop("last_action", None)
        for ev in m["raw_events"]:
            ev["action_time_display"] = _myt_short(ev["action_time"])
        m["first_join_display"] = _myt_short(m["first_join"])
        m["last_activity_display"] = _myt_short(m["last_activity"])

    total_seconds = sum(m["today_total_seconds"] for m in sorted_members)
    total_members = len(sorted_members)
    online_count = sum(1 for m in sorted_members if m["status"] == "online")
    avg_seconds = total_seconds // total_members if total_members > 0 else 0

    return {
        "ok": True,
        "total_members": total_members,
        "online_count": online_count,
        "offline_count": total_members - online_count,
        "total_duration": _fmt_dur(int(total_seconds)),
        "avg_duration": _fmt_dur(int(avg_seconds)),
        "date": today_start_myt.strftime("%Y-%m-%d"),
        "members": sorted_members,
    }


# ── seen_emails ──────────────────────────────────────────────────────────────

def check_new_email(email: str, name: str, now: datetime) -> bool:
    """返回 True 表示新人，False 表示已见过"""
    if not email:
        return False
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM seen_emails WHERE email = ?", (email,)
    ).fetchone()
    if row:
        conn.execute(
            "UPDATE seen_emails SET last_seen = ?, seen_count = seen_count + 1 WHERE email = ?",
            (now.isoformat(), email),
        )
        conn.commit()
        return False
    conn.execute(
        "INSERT INTO seen_emails (email, name, first_seen, last_seen) VALUES (?, ?, ?, ?)",
        (email, name, now.isoformat(), now.isoformat()),
    )
    conn.commit()
    return True


# ── alerts ───────────────────────────────────────────────────────────────────

def log_webhook_push(
    tenant_id: str, event_type: str, title: str, message: str,
    target_channel_id: int, telegram_chat_id: str, status: str, error_message: str = "",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO alerts (alert_type, severity, title, message, tenant_id, "
        "event_type, target_channel_id, telegram_chat_id, status, error_message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("webhook_push", "info", title, message, tenant_id,
         event_type, target_channel_id, telegram_chat_id, status, error_message,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def get_telegram_rule_by_event(event_type: str) -> dict | None:
    """根据 event_type 查询 telegram_alert_rules"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM telegram_alert_rules WHERE event_type = ?",
        (event_type,),
    ).fetchone()
    return dict(row) if row else None


def create_alert(
    alert_type: str, title: str, message: str = "",
    severity: str = "info", related_name: str = "", related_email: str = "",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO alerts (alert_type, severity, title, message, related_name, related_email) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (alert_type, severity, title, message, related_name, related_email),
    )
    conn.commit()
    return cur.lastrowid


def log_alert_sent(alert_id: int, sent_to: str, success: bool, error: str = ""):
    conn = _get_conn()
    conn.execute(
        "UPDATE alerts SET sent_to = ?, success = ? WHERE id = ?",
        (sent_to, 1 if success else 0, alert_id),
    )
    conn.commit()


def log_webhook_push(
    tenant_id: str, event_type: str, title: str, message: str,
    target_channel_id: int, telegram_chat_id: str, status: str, error_message: str = "",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO alerts (alert_type, severity, title, message, tenant_id, "
        "event_type, target_channel_id, telegram_chat_id, status, error_message, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("webhook_push", "info", title, message, tenant_id,
         event_type, target_channel_id, telegram_chat_id, status, error_message,
         datetime.now(timezone.utc).isoformat()),
    )
    conn.commit()
    return cur.lastrowid


def get_telegram_rule_by_event(event_type: str) -> dict | None:
    # 根据 event_type 查询 telegram_alert_rules
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM telegram_alert_rules WHERE event_type = ?",
        (event_type,),
    ).fetchone()
    return dict(row) if row else None


def get_recent_alerts(limit: int = 50, alert_type: str = None, tenant_id: str = None) -> list[dict]:
    conn = _get_conn()
    params = []
    clauses = []
    if alert_type:
        clauses.append("alert_type = ?")
        params.append(alert_type)
    if tenant_id:
        clauses.append("tenant_id = ?")
        params.append(tenant_id)
    where = "WHERE " + " AND ".join(clauses) if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM alerts {where} ORDER BY id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [dict(r) for r in rows]


# ── settings ─────────────────────────────────────────────────────────────────

def get_setting(key: str, default: str = None) -> str | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT value FROM settings WHERE key = ?", (key,)
    ).fetchone()
    return row[0] if row else default


def set_setting(key: str, value: str):
    conn = _get_conn()
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def init_bot_state():
    """兼容旧 command_bot 导出的初始化函数（settings 表已有，只需插入默认值）"""
    for k, v in [("push_enabled", "1"), ("quiet_mode", "0")]:
        set_setting(k, v)


def get_bot_state(key: str, default: str = "0") -> str:
    return get_setting(key, default)


def is_push_enabled() -> bool:
    return get_bot_state("push_enabled", "1") == "1"


def is_quiet_mode() -> bool:
    return get_bot_state("quiet_mode", "0") == "1"


def set_bot_state(key: str, value: str):
    set_setting(key, value)



_display_cache = {"mapping": {}, "ts": 0, "tenant_id": None}

def resolve_display_name(raw_name: str, tenant_id: str = None) -> dict:
    """返回 {display_name, count_enabled, raw_name}"""
    import time, re
    now = time.time()
    if not _display_cache["mapping"] or now - _display_cache["ts"] > 30 or _display_cache.get("tenant_id") != tenant_id:
        conn = _get_conn()
        if tenant_id:
            rows = conn.execute("SELECT raw_name, display_name, match_key, count_enabled, aliases FROM member_display WHERE tenant_id = ?", (tenant_id,)).fetchall()
        else:
            rows = conn.execute("SELECT raw_name, display_name, match_key, count_enabled, aliases FROM member_display").fetchall()
        _display_cache["mapping"] = {
            r[0]: {"display": r[1], "key": r[2], "enabled": bool(r[3]), "aliases": json.loads(r[4] or "[]")}
            for r in rows
        }
        _display_cache["ts"] = now
        _display_cache["tenant_id"] = tenant_id
    
    name = raw_name.strip()
    if not name:
        return {"display_name": "", "count_enabled": True, "raw_name": name}
    
    mapping = _display_cache["mapping"]
    
    # 1. Exact match on raw_name
    if name in mapping:
        m = mapping[name]
        return {"display_name": m["display"], "count_enabled": m["enabled"], "raw_name": name}

    # 2. Match on match_key (lowercase, no spaces)
    key = re.sub(r'\s+', '', name.lower())
    for raw, m in mapping.items():
        if m["key"] == key:
            return {"display_name": m["display"], "count_enabled": m["enabled"], "raw_name": name}

    # 3. Match on aliases (曾用名匹配)
    name_lower = name.lower().replace(" ", "")
    for raw, m in mapping.items():
        if name_lower in [a.lower().replace(" ", "") for a in m.get("aliases", [])]:
            return {"display_name": m["display"], "count_enabled": m["enabled"], "raw_name": name}

    # 4. No match — try dedup: check if another entry exists with same match_key
    if not mapping:
        # mapping is empty, just return as-is
        return {"display_name": name, "count_enabled": True, "raw_name": name}
    for raw, m in mapping.items():
        if m["key"] == key:
            return {"display_name": m["display"], "count_enabled": m["enabled"], "raw_name": name}

    # 5. Last resort: return as-is
    return {"display_name": name, "count_enabled": True, "raw_name": name}

def log_command(chat_id: str, command: str, args: str = "", response: str = ""):
    conn = _get_conn()
    # 简化为直接写入 alerts 表
    create_alert(
        alert_type="command",
        title=f"/{command}",
        message=f"{chat_id}: {args} → {response[:100]}",
        severity="info",
    )


# ── Telegram Alert Rules ─────────────────────────────────────────────────────

DEFAULT_TELEGRAM_RULES = [
    {"event_type": "participant_joined",               "title": "成员加入会议",       "enabled": 1},
    {"event_type": "participant_left",                  "title": "成员离开会议",       "enabled": 1},
    {"event_type": "sharing_started",                   "title": "开始共享屏幕",       "enabled": 1},
    {"event_type": "sharing_ended",                     "title": "结束共享屏幕",       "enabled": 1},
    {"event_type": "sharing_timeout",                   "title": "共享超时",           "enabled": 1},
    {"event_type": "unknown_user",                      "title": "陌生人进入",         "enabled": 1},
    {"event_type": "participant_joined_breakout_room",  "title": "加入分组讨论室",     "enabled": 0},
    {"event_type": "participant_left_breakout_room",    "title": "离开分组讨论室",     "enabled": 0},
    {"event_type": "participant_joined_waiting_room",   "title": "有人在等候室",       "enabled": 1},
    {"event_type": "frequent_join_leave",                 "title": "短时间频繁进出",     "enabled": 1},
    {"event_type": "periodic_online_report",                 "title": "定时在线报告（每3小时）", "enabled": 1},
]


def seed_telegram_rules():
    """插入默认 Telegram 告警规则，INSERT OR IGNORE 防止重复"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    for rule in DEFAULT_TELEGRAM_RULES:
        _cd = 300 if rule["event_type"] in ("participant_joined", "participant_left") else 60
        conn.execute(
            "INSERT OR IGNORE INTO telegram_alert_rules "
            "(event_type, title, enabled, cooldown_seconds, quiet_enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, 0, ?, ?)",
            (rule["event_type"], rule["title"], rule["enabled"], _cd, now, now),
        )
    conn.commit()


def get_telegram_rules() -> list[dict]:
    """获取所有 Telegram 告警规则"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM telegram_alert_rules ORDER BY event_type"
    ).fetchall()
    return [dict(r) for r in rows]


def get_telegram_rules_by_tenant(tenant_id: str) -> list[dict]:
    """获取指定租户的 Telegram 告警规则（fallback 到默认规则）"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM telegram_alert_rules WHERE tenant_id = ? ORDER BY event_type",
        (tenant_id,),
    ).fetchall()
    # If tenant has no rules yet, return default rules
    if not rows:
        rows = conn.execute(
            "SELECT * FROM telegram_alert_rules WHERE tenant_id = 'default' ORDER BY event_type"
        ).fetchall()
    return [dict(r) for r in rows]


def get_alert_rule_channels(event_type: str) -> list[int]:
    """获取告警规则关联的 channel_id 列表"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT channel_id FROM alert_rule_channels WHERE event_type = ?",
        (event_type,),
    ).fetchall()
    return [r[0] for r in rows]


def set_alert_rule_channels(event_type: str, channel_ids: list[int]):
    """设置告警规则关联的 channel_id 列表（全量覆盖）"""
    conn = _get_conn()
    conn.execute("DELETE FROM alert_rule_channels WHERE event_type = ?", (event_type,))
    for cid in channel_ids:
        conn.execute(
            "INSERT OR IGNORE INTO alert_rule_channels (event_type, channel_id) VALUES (?, ?)",
            (event_type, cid),
        )
    conn.commit()


def get_rules_with_channels(tenant_id: str = "default") -> list[dict]:
    """获取告警规则列表，每条规则附带 channels 字段"""
    rules = get_telegram_rules_by_tenant(tenant_id)
    for r in rules:
        r["channel_ids"] = get_alert_rule_channels(r["event_type"])
    return rules


def get_telegram_rule(event_type: str) -> dict | None:
    """获取指定 event_type 的告警规则，不存在返回 None"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM telegram_alert_rules WHERE event_type = ?",
        (event_type,),
    ).fetchone()
    return dict(row) if row else None


def upsert_telegram_rule(event_type: str, data: dict) -> int:
    """插入或更新告警规则，返回 id"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()

    # 检查是否已存在
    existing = conn.execute(
        "SELECT id FROM telegram_alert_rules WHERE event_type = ?",
        (event_type,),
    ).fetchone()

    if existing:
        fields = []
        values = []
        for key in ("title", "enabled", "cooldown_seconds",
                     "quiet_enabled", "quiet_start", "quiet_end",
                     "target_channel_id"):
            if key in data:
                fields.append(f"{key} = ?")
                values.append(data[key])
        fields.append("updated_at = ?")
        values.append(now)
        values.append(event_type)
        conn.execute(
            f"UPDATE telegram_alert_rules SET {', '.join(fields)} WHERE event_type = ?",
            values,
        )
        conn.commit()
        log_audit("update", "telegram_alert_rule", existing[0],
                  f"Updated rule for {event_type}")
        return existing[0]
    else:
        cur = conn.execute(
            "INSERT INTO telegram_alert_rules "
            "(event_type, title, enabled, cooldown_seconds, "
            " quiet_enabled, quiet_start, quiet_end, target_channel_id, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_type,
                data.get("title", ""),
                data.get("enabled", 1),
                data.get("cooldown_seconds", 60),
                data.get("quiet_enabled", 0),
                data.get("quiet_start", "00:00"),
                data.get("quiet_end", "08:00"),
                data.get("target_channel_id"),
                now,
                now,
            ),
        )
        conn.commit()
        log_audit("create", "telegram_alert_rule", cur.lastrowid,
                  f"Created rule for {event_type}")
        return cur.lastrowid


def delete_telegram_rule(event_type: str) -> bool:
    """删除指定 event_type 的告警规则，返回是否成功删除"""
    conn = _get_conn()
    existing = conn.execute(
        "SELECT id FROM telegram_alert_rules WHERE event_type = ?",
        (event_type,),
    ).fetchone()
    if not existing:
        return False
    conn.execute(
        "DELETE FROM telegram_alert_rules WHERE event_type = ?",
        (event_type,),
    )
    conn.commit()
    log_audit("delete", "telegram_alert_rule", existing[0],
              f"Deleted rule for {event_type}")
    return True


def update_rule_test_result(event_type: str, ok: bool, error: str = ""):
    """记录规则的最近一次测试结果"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE telegram_alert_rules SET last_test_at=?, last_test_result=?, last_test_error=? WHERE event_type=?",
        (now, "ok" if ok else "fail", error, event_type),
    )
    conn.commit()


def _is_within_quiet_hours(rule: dict) -> bool:
    """判断当前 MYT (UTC+8) 时间是否在静默时段内"""
    from datetime import time as dt_time
    myt_now = datetime.now(timezone.utc) + timedelta(hours=8)
    current = myt_now.time()

    try:
        start_parts = rule["quiet_start"].split(":")
        end_parts = rule["quiet_end"].split(":")
        start = dt_time(int(start_parts[0]), int(start_parts[1]))
        end = dt_time(int(end_parts[0]), int(end_parts[1]))
    except (ValueError, IndexError, KeyError):
        return False

    if start <= end:
        # 正常区间（如 00:00~08:00）
        return start <= current <= end
    else:
        # 跨天区间（如 22:00~06:00）
        return current >= start or current <= end


def should_send_telegram(event_type: str) -> bool:
    """判断是否应该发送 Telegram 通知

    逻辑：
    1. 查规则，不存在则返回 True（兼容旧逻辑）
    2. not enabled → False
    3. cooldown: 查 alert_sent 表同一 event_type 最近一次发送时间
    4. quiet_hours: 判断当前 MYT 时间是否在静默时段内
    """
    conn = _get_conn()
    rule = conn.execute(
        "SELECT * FROM telegram_alert_rules WHERE event_type = ?",
        (event_type,),
    ).fetchone()

    # 1. 规则不存在 → 兼容旧逻辑，允许发送
    if not rule:
        return True

    rule = dict(rule)

    # 2. 未启用
    if not rule["enabled"]:
        return False

    # 3. Cooldown: 查 alert_sent 表，alert_key 格式为 "telegram:{event_type}"
    alert_key = f"telegram:{event_type}"
    last_sent_row = conn.execute(
        "SELECT sent_at FROM alert_sent WHERE alert_key = ? ORDER BY id DESC LIMIT 1",
        (alert_key,),
    ).fetchone()

    if last_sent_row and last_sent_row["sent_at"]:
        try:
            last_sent = datetime.fromisoformat(last_sent_row["sent_at"])
            now = datetime.now(timezone.utc)
            elapsed = (now - last_sent).total_seconds()
            cooldown = rule.get("cooldown_seconds", 0)
            if cooldown > 0 and elapsed < cooldown:
                return False
        except (ValueError, TypeError):
            pass

    # 4. Quiet hours
    if rule.get("quiet_enabled", 0):
        if _is_within_quiet_hours(rule):
            return False

    # 5. 允许发送
    return True


def log_audit(action: str, entity_type: str = "telegram_alert_rule",
              entity_id: int = None, details: str = ""):
    """写入审计日志"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO audit_logs (action, entity_type, entity_id, details, created_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (action, entity_type, entity_id, details, now),
    )
    conn.commit()


def get_rule_push_target(event_type: str) -> tuple[str, str]:
    """根据告警规则 event_type 查询推送目标和 Bot Token
    
    返回 (bot_token, chat_id)
    - bot_token 为空字符串表示使用全局 Bot
    - chat_id 为空字符串表示没有配置目标
    - 规则不存在或未启用也返回 ("", "")
    """
    conn = _get_conn()
    rule = conn.execute(
        "SELECT * FROM telegram_alert_rules WHERE event_type = ?",
        (event_type,),
    ).fetchone()
    if not rule:
        return ("", "")
    rule = dict(rule)
    if not rule.get("enabled", 0):
        return ("", "")
    
    target_channel_id = rule.get("target_channel_id")
    if not target_channel_id:
        # 没有指定频道，用默认行为
        return ("", "")
    
    ch = conn.execute(
        "SELECT bot_token, chat_id FROM telegram_channels WHERE id = ?",
        (target_channel_id,),
    ).fetchone()
    if not ch:
        return ("", "")
    
    bot_token = ch["bot_token"] or ""
    chat_id = ch["chat_id"] or ""
    return (bot_token, chat_id)


# ── Telegram Channels ─────────────────────────────────────────────────────

def _seed_default_telegram_channel():
    """插入默认 Telegram 频道"""
    conn = _get_conn()
    existing = conn.execute("SELECT id FROM telegram_channels WHERE name = ?", ("默认",)).fetchone()
    if not existing:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO telegram_channels (name, chat_id, enabled, is_default, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("默认", "7922047310", 1, 1, "", now, now),
        )
        conn.commit()


def get_telegram_channels() -> list[dict]:
    """获取所有 Telegram 频道"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM telegram_channels ORDER BY is_default DESC, name"
    ).fetchall()
    return [dict(r) for r in rows]


def get_telegram_channel(chat_id: str) -> dict | None:
    """获取指定 chat_id 的频道"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM telegram_channels WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    return dict(row) if row else None


def get_telegram_channel_by_id(channel_id: int) -> dict | None:
    """获取指定 id 的频道"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM telegram_channels WHERE id = ?",
        (channel_id,),
    ).fetchone()
    return dict(row) if row else None


def get_default_channel() -> dict | None:
    """获取默认频道 (is_default=1)"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM telegram_channels WHERE is_default = 1 AND enabled = 1 LIMIT 1"
    ).fetchone()
    return dict(row) if row else None


def upsert_telegram_channel(data: dict) -> int:
    """插入或更新频道，如果 name 已存在则 UPDATE，否则 INSERT"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    name = data.get("name", "").strip()
    chat_id = data.get("chat_id", "").strip()

    if not name or not chat_id:
        raise ValueError("name and chat_id are required")

    existing = conn.execute(
        "SELECT id FROM telegram_channels WHERE name = ?",
        (name,),
    ).fetchone()

    if existing:
        # Update
        fields = []
        values = []
        for key in ("chat_id", "enabled", "is_default", "notes", "bot_token", "bot_username"):
            if key in data:
                fields.append(f"{key} = ?")
                values.append(data[key])
        if fields:
            fields.append("updated_at = ?")
            values.append(now)
            values.append(existing[0])
            conn.execute(
                f"UPDATE telegram_channels SET {', '.join(fields)} WHERE id = ?",
                values,
            )
        conn.commit()
        log_audit("update", "telegram_channel", existing[0],
                  f"Updated channel: {name} (chat_id={chat_id})")
        return existing[0]
    else:
        # If is_default, unset other defaults
        if data.get("is_default", 0):
            conn.execute("UPDATE telegram_channels SET is_default = 0")

        cur = conn.execute(
            "INSERT INTO telegram_channels (name, chat_id, enabled, is_default, notes, "
            "bot_token, bot_username, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                chat_id,
                data.get("enabled", 1),
                data.get("is_default", 0),
                data.get("notes", ""),
                data.get("bot_token", ""),
                data.get("bot_username", ""),
                now,
                now,
            ),
        )
        conn.commit()
        log_audit("create", "telegram_channel", cur.lastrowid,
                  f"Created channel: {name} (chat_id={chat_id})")
        return cur.lastrowid


def delete_telegram_channel(chat_id: str) -> bool:
    """删除指定 chat_id 的频道"""
    conn = _get_conn()
    existing = conn.execute(
        "SELECT id, name FROM telegram_channels WHERE chat_id = ?",
        (chat_id,),
    ).fetchone()
    if not existing:
        return False
    conn.execute(
        "DELETE FROM telegram_channels WHERE chat_id = ?",
        (chat_id,),
    )
    conn.commit()
    log_audit("delete", "telegram_channel", existing[0],
              f"Deleted channel: {existing[1]} (chat_id={chat_id})")
    return True




# ── Member Groups ─────────────────────────────────────────────────────────

DEFAULT_MEMBER_GROUPS = [
    {"name": "核销", "description": "核销组成员"},
    {"name": "推进", "description": "推进组成员"},
]


def seed_member_groups():
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    for g in DEFAULT_MEMBER_GROUPS:
        conn.execute(
            "INSERT OR IGNORE INTO member_groups (name, description, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (g["name"], g["description"], now, now),
        )
    conn.commit()


def get_member_group(member_name: str, tenant_id: str = None) -> str | None:
    if not member_name:
        return None
    conn = _get_conn()
    name = member_name.strip().lower().replace(" ", "")
    # 优先从 member_display.group_id 读取（新方式）
    if tenant_id:
        row = conn.execute(
            "SELECT g.name FROM member_groups g "
            "JOIN member_display md ON md.group_id = g.id "
            "WHERE (REPLACE(LOWER(TRIM(md.raw_name)), ' ', '') = ? "
            "   OR REPLACE(LOWER(TRIM(md.display_name)), ' ', '') = ?) "
            "AND md.group_id IS NOT NULL AND md.tenant_id = ?",
            (name, name, tenant_id),
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT g.name FROM member_groups g "
            "JOIN member_display md ON md.group_id = g.id "
            "WHERE (REPLACE(LOWER(TRIM(md.raw_name)), ' ', '') = ? "
            "   OR REPLACE(LOWER(TRIM(md.display_name)), ' ', '') = ?) "
            "AND md.group_id IS NOT NULL",
            (name, name),
        ).fetchone()
    if row:
        return row[0]
    # 回退：兼容旧 member_group_members 表数据
    row = conn.execute(
        "SELECT g.name FROM member_groups g "
        "JOIN member_group_members m ON m.group_id = g.id "
        "WHERE REPLACE(LOWER(TRIM(m.member_name)), ' ', '') = ?",
        (name,),
    ).fetchone()
    return row[0] if row else None


def get_all_groups(tenant_id: str = None) -> list[dict]:
    conn = _get_conn()
    if tenant_id:
        groups = conn.execute(
            "SELECT * FROM member_groups WHERE tenant_id = ? ORDER BY name",
            (tenant_id,)
        ).fetchall()
    else:
        groups = conn.execute(
            "SELECT * FROM member_groups ORDER BY name"
        ).fetchall()
    result = []
    for g in groups:
        gd = dict(g)
        # 新方式优先：从 member_display.group_id 读取
        members = conn.execute(
            "SELECT display_name FROM member_display WHERE group_id = ? ORDER BY display_name",
            (gd["id"],),
        ).fetchall()
        if not members:
            # 回退旧表
            members = conn.execute(
                "SELECT member_name FROM member_group_members WHERE group_id = ? ORDER BY member_name",
                (gd["id"],),
            ).fetchall()
        gd["members"] = [m[0] for m in members]
        result.append(gd)
    return result


def add_member_to_group(group_id: int, member_name: str) -> bool:
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    try:
        # 新方式：写入 member_display.group_id
        # 先确保 member_display 中有该成员
        existing = conn.execute(
            "SELECT id FROM member_display WHERE raw_name = ?",
            (member_name.strip(),),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE member_display SET group_id = ?, updated_at = ? WHERE id = ?",
                (group_id, now, existing[0]),
            )
        else:
            match_key = member_name.strip().lower().replace(" ", "")
            # 检查 match_key 是否已被其他记录使用
            existing_by_key = conn.execute(
                "SELECT id, raw_name FROM member_display WHERE match_key = ? AND raw_name != ?",
                (match_key, member_name.strip()),
            ).fetchone()
            if existing_by_key:
                # 有同 match_key 的记录，直接复用（改名 + 更新 group_id）
                conn.execute(
                    "UPDATE member_display SET raw_name = ?, display_name = ?, group_id = ?, updated_at = ? WHERE id = ?",
                    (member_name.strip(), existing_by_key[1], group_id, now, existing_by_key[0]),
                )
            else:
                conn.execute(
                    "INSERT INTO member_display (raw_name, display_name, match_key, group_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (member_name.strip(), member_name.strip(), match_key, group_id, now, now),
                )
        # 旧方式：同时写入 member_group_members 保持兼容
        conn.execute(
            "INSERT OR IGNORE INTO member_group_members (group_id, member_name, created_at) VALUES (?, ?, ?)",
            (group_id, member_name.strip(), now),
        )
        conn.commit()
        log_audit("create", "member_group_member", group_id,
                  f"Added member {member_name} to group {group_id}")
        return True
    except Exception:
        return False


def remove_member_from_group(group_id: int, member_name: str) -> bool:
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM member_group_members WHERE group_id = ? AND member_name = ?",
            (group_id, member_name.strip()),
        )
        conn.commit()
        log_audit("delete", "member_group_member", group_id,
                  f"Removed member {member_name} from group {group_id}")
        return True
    except Exception:
        return False


def delete_member_group(group_id: int) -> bool:
    conn = _get_conn()
    try:
        conn.execute("DELETE FROM member_group_members WHERE group_id = ?", (group_id,))
        conn.execute("DELETE FROM member_groups WHERE id = ?", (group_id,))
        conn.commit()
        log_audit("delete", "member_group", group_id, "Deleted group")
        return True
    except Exception:
        return False


def update_member_group(group_id: int, name: str, description: str = "") -> bool:
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "UPDATE member_groups SET name = ?, description = ?, updated_at = ? WHERE id = ?",
            (name, description, now, group_id),
        )
        conn.commit()
        log_audit("update", "member_group", group_id, f"Updated group: {name}")
        return True
    except Exception:
        return False

def normalize_identity_name(name: str) -> str:
    """归一化姓名：去空格、大小写、连字符"""
    import re
    return re.sub(r"[\s\-\._']+", "", (name or "").strip().lower())

import secrets
import hashlib

# ── bcrypt wrapper (fallback to hashlib if bcrypt not available) ─────────
try:
    import bcrypt as _bcrypt
    def _hash_pw(pw: str) -> str:
        return _bcrypt.hashpw(pw.encode(), _bcrypt.gensalt()).decode()
    def _check_pw(pw: str, hashed: str) -> bool:
        return _bcrypt.checkpw(pw.encode(), hashed.encode())
except ImportError:
    import hashlib, os
    def _hash_pw(pw: str) -> str:
        salt = os.urandom(16).hex()
        h = hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100000).hex()
        return f"{salt}${h}"
    def _check_pw(pw: str, hashed: str) -> bool:
        try:
            salt, h = hashed.split('$', 1)
            return hashlib.pbkdf2_hmac('sha256', pw.encode(), salt.encode(), 100000).hex() == h
        except:
            return False


# ── Migration: Add tenant_id to existing tables ─────────────────────────
MT_MIGRATIONS = [
    "ALTER TABLE zoom_events ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE zoom_participants ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE seen_emails ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE alerts ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE sharing_live ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE alert_rules ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE zoom_events ADD COLUMN owner_id INTEGER DEFAULT NULL",
    "ALTER TABLE zoom_participants ADD COLUMN owner_id INTEGER DEFAULT NULL",
    "ALTER TABLE seen_emails ADD COLUMN owner_id INTEGER DEFAULT NULL",
    "ALTER TABLE alerts ADD COLUMN owner_id INTEGER DEFAULT NULL",
    "ALTER TABLE sharing_live ADD COLUMN owner_id INTEGER DEFAULT NULL",
    "ALTER TABLE alert_rules ADD COLUMN owner_id INTEGER DEFAULT NULL",
    "ALTER TABLE telegram_alert_rules ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE telegram_channels ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE member_groups ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE member_display ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE member_group_members ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE audit_logs ADD COLUMN tenant_id TEXT DEFAULT 'default'",
    "ALTER TABLE tenant_channels ADD COLUMN bot_token TEXT DEFAULT ''",
    "ALTER TABLE tenants ADD COLUMN telegram_bot_token TEXT DEFAULT ''",
    "ALTER TABLE tenants ADD COLUMN telegram_bot_username TEXT DEFAULT ''",
    "ALTER TABLE tenants ADD COLUMN telegram_bot_verified_at TEXT DEFAULT ''",
]

MT_TABLES = [
    "CREATE TABLE IF NOT EXISTS users ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  username TEXT NOT NULL UNIQUE,"
    "  password_hash TEXT NOT NULL,"
    "  display_name TEXT NOT NULL DEFAULT '',"
    "  role TEXT NOT NULL DEFAULT 'viewer',"  # super_admin / tenant_admin / viewer
    "  is_active INTEGER NOT NULL DEFAULT 1,"
    "  created_at TEXT NOT NULL,"
    "  updated_at TEXT NOT NULL,"
    "  zoom_plan TEXT NOT NULL DEFAULT 'unknown',"
    "  live_mode TEXT NOT NULL DEFAULT 'metrics',"
    "  sharing_mode TEXT NOT NULL DEFAULT 'metrics',"
    "  report_mode TEXT NOT NULL DEFAULT 'enabled',"
    "  metrics_available INTEGER NOT NULL DEFAULT 0,"
    "  reports_available INTEGER NOT NULL DEFAULT 0"
    ")",

    "CREATE TABLE IF NOT EXISTS tenants ("
    "  id TEXT PRIMARY KEY,"  # slug-based: 'default', client-xxx
    "  name TEXT NOT NULL,"
    "  display_name TEXT NOT NULL DEFAULT '',"
    "  plan TEXT NOT NULL DEFAULT 'pro',"
    "  is_active INTEGER NOT NULL DEFAULT 1,"
    "  is_global_admin INTEGER NOT NULL DEFAULT 0,"
    "  api_token TEXT NOT NULL DEFAULT '',"
    "  created_at TEXT NOT NULL,"
    "  updated_at TEXT NOT NULL,"
    "  zoom_plan TEXT NOT NULL DEFAULT 'unknown',"
    "  live_mode TEXT NOT NULL DEFAULT 'metrics',"
    "  sharing_mode TEXT NOT NULL DEFAULT 'metrics',"
    "  report_mode TEXT NOT NULL DEFAULT 'enabled',"
    "  metrics_available INTEGER NOT NULL DEFAULT 0,"
    "  reports_available INTEGER NOT NULL DEFAULT 0"
    ")",

    "CREATE TABLE IF NOT EXISTS tenant_users ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  user_id INTEGER NOT NULL REFERENCES users(id),"
    "  tenant_id TEXT NOT NULL REFERENCES tenants(id),"
    "  role TEXT NOT NULL DEFAULT 'viewer',"  # owner / admin / viewer
    "  is_active INTEGER NOT NULL DEFAULT 1,"
    "  created_at TEXT NOT NULL,"
    "  UNIQUE(user_id, tenant_id)"
    ")",

    "CREATE TABLE IF NOT EXISTS zoom_accounts ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  tenant_id TEXT NOT NULL REFERENCES tenants(id),"
    "  label TEXT NOT NULL DEFAULT '',"
    "  account_id TEXT NOT NULL,"
    "  client_id TEXT NOT NULL,"
    "  client_secret TEXT NOT NULL,"
    "  host_email TEXT NOT NULL DEFAULT '',"
    "  is_active INTEGER NOT NULL DEFAULT 1,"
    "  created_at TEXT NOT NULL"
    ")",

    "CREATE TABLE IF NOT EXISTS monitored_meetings ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  tenant_id TEXT NOT NULL REFERENCES tenants(id),"
    "  account_id INTEGER REFERENCES zoom_accounts(id),"
    "  meeting_id TEXT NOT NULL,"
    "  label TEXT NOT NULL DEFAULT '',"
    "  meeting_type TEXT NOT NULL DEFAULT 'pmi',"
    "  is_active INTEGER NOT NULL DEFAULT 1,"
    "  created_at TEXT NOT NULL"
    ")",

    "CREATE TABLE IF NOT EXISTS tenant_channels ("
    "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
    "  tenant_id TEXT NOT NULL REFERENCES tenants(id),"
    "  chat_id TEXT NOT NULL,"
    "  label TEXT NOT NULL DEFAULT '',"
    "  is_group INTEGER NOT NULL DEFAULT 0,"
    "  is_enabled INTEGER NOT NULL DEFAULT 1,"
    "  created_at TEXT NOT NULL"
    ")",
]


def run_mt_migrations(readonly: bool = False):
    """Run multi-tenant migrations on existing tables + create new tables."""
    if readonly:
        return
    conn = _get_conn()
    for sql in MT_TABLES:
        conn.execute(sql)
    for sql in MT_MIGRATIONS:
        try:
            conn.execute(sql)
        except Exception:
            pass  # column already exists

    # Seed default tenant if not exists
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO tenants (id, name, display_name, plan, is_active, is_global_admin, api_token, zoom_plan, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("default", "Default", "默认租户", "business", 1, 1, "", "business", now, now)
        )
    except Exception:
        pass

    # ── 默认租户能力初始化 ──
    try:
        conn.execute(
            "UPDATE tenants SET zoom_plan='business', live_mode='metrics', sharing_mode='metrics', "
            "metrics_available=1, reports_available=0, report_mode='enabled' WHERE id='default' AND zoom_plan='unknown'"
        )
    except Exception:
        pass
    conn.commit()


# ── Auth Functions ──────────────────────────────────────────────────────

def create_user(username: str, password: str, display_name: str = "",
                role: str = "super_admin", tenant_id: str = "default") -> int:
    """Create user. Returns user id. Raises on duplicate username."""
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    pw_hash = _hash_pw(password)
    cur = conn.execute(
        "INSERT INTO users (username, password_hash, display_name, role, is_active, tenant_id, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, ?, ?, ?)",
        (username, pw_hash, display_name, role, tenant_id, now, now),
    )
    conn.commit()
    return cur.lastrowid


def get_user_by_username(username: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE username = ?", (username,)
    ).fetchone()
    return dict(row) if row else None


def get_user_by_id(user_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    return dict(row) if row else None


def verify_user_password(username: str, password: str) -> dict | None:
    """Verify user credentials. Returns user dict on success, None on failure."""
    user = get_user_by_username(username)
    if not user:
        return None
    if not user["is_active"]:
        return None
    if not _check_pw(password, user["password_hash"]):
        return None
    return user


def get_user_tenants(user_id: int) -> list[dict]:
    """Get all tenant_ids this user belongs to (active only)."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT tu.tenant_id, tu.role as tenant_role, "
        "       t.name, t.display_name, t.plan, t.is_active "
        "FROM tenant_users tu "
        "JOIN tenants t ON t.id = tu.tenant_id "
        "WHERE tu.user_id = ? AND tu.is_active = 1 AND t.is_active = 1",
        (user_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def set_user_tenant_role(user_id: int, tenant_id: str, role: str) -> bool:
    """Add or update user-tenant association."""
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO tenant_users (user_id, tenant_id, role, is_active, created_at) "
            "VALUES (?, ?, ?, 1, ?) "
            "ON CONFLICT(user_id, tenant_id) DO UPDATE SET role = ?, is_active = 1",
            (user_id, tenant_id, role, now, role),
        )
        conn.commit()
        return True
    except Exception:
        return False


def remove_user_from_tenant(user_id: int, tenant_id: str) -> bool:
    conn = _get_conn()
    try:
        conn.execute(
            "DELETE FROM tenant_users WHERE user_id = ? AND tenant_id = ?",
            (user_id, tenant_id),
        )
        conn.commit()
        return True
    except Exception:
        return False


# ── Tenant CRUD ─────────────────────────────────────────────────────────

def create_tenant(name: str, display_name: str = "", plan: str = "pro") -> str:
    """Create tenant. Returns tenant_id (slug)."""
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    # Generate unique slug
    import re
    slug = re.sub(r'[^a-z0-9-]', '', name.lower().replace(' ', '-'))
    if not slug:
        slug = "tenant"
    base = slug
    counter = 1
    while conn.execute("SELECT 1 FROM tenants WHERE id = ?", (slug,)).fetchone():
        slug = f"{base}-{counter}"
        counter += 1
    api_token = secrets.token_hex(24)
    conn.execute(
        "INSERT INTO tenants (id, name, display_name, plan, is_active, is_global_admin, api_token, zoom_plan, created_at, updated_at) "
        "VALUES (?, ?, ?, ?, 1, 0, ?, ?, ?, ?)",
        (slug, name, display_name or name, plan, api_token, "unknown", now, now),
    )
    conn.commit()
    log_audit("create", "tenant", 0, f"Created tenant: {slug}")
    return slug


def get_all_tenants() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tenants ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_tenant(tenant_id: str) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    return dict(row) if row else None


def update_tenant_capabilities(tenant_id: str, capabilities: dict) -> bool:
    """Update tenant capability fields after Zoom metadata detection."""
    conn = _get_conn()
    fields = []
    vals = []
    for key in ("zoom_plan", "live_mode", "sharing_mode", "report_mode",
                 "metrics_available", "reports_available"):
        if key in capabilities:
            fields.append(f"{key}=?")
            vals.append(capabilities[key])
    if not fields:
        return False
    vals.append(tenant_id)
    from datetime import datetime, timezone
    conn.execute(
        f"UPDATE tenants SET {', '.join(fields)}, updated_at=? WHERE id=?",
        (*vals, datetime.now(timezone.utc).isoformat(), tenant_id),
    )
    conn.commit()
    return True


def toggle_tenant(tenant_id: str) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT is_active FROM tenants WHERE id = ?", (tenant_id,)).fetchone()
    if not row:
        return False
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE tenants SET is_active = ? WHERE id = ?", (new_val, tenant_id))
    conn.commit()
    log_audit("update", "tenant", 0, f"Toggled tenant {tenant_id}: active={new_val}")
    return True


def delete_tenant(tenant_id: str) -> bool:
    if tenant_id == "default":
        return False
    conn = _get_conn()
    conn.execute("DELETE FROM tenant_users WHERE tenant_id = ?", (tenant_id,))
    conn.execute("DELETE FROM zoom_accounts WHERE tenant_id = ?", (tenant_id,))
    conn.execute("DELETE FROM monitored_meetings WHERE tenant_id = ?", (tenant_id,))
    conn.execute("DELETE FROM tenant_channels WHERE tenant_id = ?", (tenant_id,))
    conn.execute("DELETE FROM tenants WHERE id = ?", (tenant_id,))
    conn.commit()
    log_audit("delete", "tenant", 0, f"Deleted tenant: {tenant_id}")
    return True


def regenerate_tenant_token(tenant_id: str) -> str:
    conn = _get_conn()
    api_token = secrets.token_hex(24)
    conn.execute("UPDATE tenants SET api_token = ? WHERE id = ?", (api_token, tenant_id))
    conn.commit()
    return api_token


def get_tenant_bot_config(tenant_id: str) -> dict:
    """Get tenant's Telegram bot config."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT telegram_bot_token, telegram_bot_username, telegram_bot_verified_at "
        "FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    if not row:
        return {"token": "", "username": "", "verified_at": ""}
    return {
        "token": row[0] or "",
        "username": row[1] or "",
        "verified_at": row[2] or "",
    }


def update_tenant_bot_config(tenant_id: str, token: str, username: str = "",
                              verified_at: str = "") -> bool:
    """Save tenant's Telegram bot config."""
    conn = _get_conn()
    conn.execute(
        "UPDATE tenants SET telegram_bot_token = ?, telegram_bot_username = ?, "
        "telegram_bot_verified_at = ? WHERE id = ?",
        (token, username, verified_at, tenant_id),
    )
    conn.commit()
    return True


def get_tenant_channels_periodic_report() -> list[dict]:
    """Get all enabled tenant_channels (is_enabled=1, bot_token non-empty) for periodic report."""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT tenant_id, chat_id, label, bot_token "
        "FROM tenant_channels "
        "WHERE is_enabled = 1 AND bot_token IS NOT NULL AND bot_token != ''"
    ).fetchall()
    return [
        {"tenant_id": row[0], "chat_id": row[1], "label": row[2], "bot_token": row[3]}
        for row in rows
    ]


# ── User CRUD (admin) ────────────────────────────────────────────────────

def get_all_users() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT u.*, "
        "  (SELECT GROUP_CONCAT(tu.tenant_id || ':' || tu.role) FROM tenant_users tu WHERE tu.user_id = u.id) as tenant_roles "
        "FROM users u ORDER BY u.created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_user(target_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT is_active FROM users WHERE id = ?", (target_id,)).fetchone()
    if not row:
        return False
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE users SET is_active = ? WHERE id = ?", (new_val, target_id))
    conn.commit()
    return True


def update_user(target_id: int, display_name: str = None, role: str = None,
                tenant_id: str = None) -> bool:
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    fields = ["updated_at = ?"]
    vals = [now]
    if display_name is not None:
        fields.append("display_name = ?")
        vals.append(display_name)
    if role is not None:
        fields.append("role = ?")
        vals.append(role)
    if tenant_id is not None:
        fields.append("tenant_id = ?")
        vals.append(tenant_id)
    vals.append(target_id)
    conn.execute(
        f"UPDATE users SET {', '.join(fields)} WHERE id = ?",
        vals,
    )
    conn.commit()
    return True


def delete_user(target_id: int) -> bool:
    conn = _get_conn()
    conn.execute("DELETE FROM tenant_users WHERE user_id = ?", (target_id,))
    conn.execute("DELETE FROM users WHERE id = ?", (target_id,))
    conn.commit()
    return True


# ── Zoom Accounts CRUD ──────────────────────────────────────────────────

def create_zoom_account(tenant_id: str, label: str, account_id: str,
                        client_id: str, client_secret: str, host_email: str = "",
                        webhook_secret: str = "") -> int:
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO zoom_accounts (tenant_id, label, account_id, client_id, client_secret, host_email, webhook_secret, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, label, account_id, client_id, client_secret, host_email, webhook_secret, now),
    )
    conn.commit()
    log_audit("create", "zoom_account", cur.lastrowid, f"Created Zoom account: {label}")
    return cur.lastrowid


def get_zoom_accounts(tenant_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM zoom_accounts WHERE tenant_id = ? ORDER BY created_at DESC",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_active_zoom_accounts() -> list[dict]:
    """获取所有租户下激活的 Zoom 账号"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT za.*, t.name AS tenant_name "
        "FROM zoom_accounts za "
        "JOIN tenants t ON t.id = za.tenant_id "
        "WHERE za.is_active = 1 AND t.is_active = 1 "
        "ORDER BY za.tenant_id, za.created_at"
    ).fetchall()
    return [dict(r) for r in rows]


def get_active_zoom_accounts_for_tenant(tenant_id: str) -> list[dict]:
    """获取指定租户下激活的 Zoom 账号"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT za.*, t.name AS tenant_name "
        "FROM zoom_accounts za "
        "JOIN tenants t ON t.id = za.tenant_id "
        "WHERE za.is_active = 1 AND t.is_active = 1 AND za.tenant_id = ? "
        "ORDER BY za.created_at",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_zoom_account(account_db_id: int) -> dict | None:
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM zoom_accounts WHERE id = ?", (account_db_id,)
    ).fetchone()
    return dict(row) if row else None


def delete_zoom_account(account_id: int) -> bool:
    conn = _get_conn()
    conn.execute("DELETE FROM zoom_accounts WHERE id = ?", (account_id,))
    conn.commit()
    log_audit("delete", "zoom_account", account_id, "Deleted Zoom account")
    return True


def update_zoom_account(db_id: int, **kwargs) -> bool:
    """Update zoom_account editable fields. Accepts: label, account_id, host_email, webhook_secret, is_active, client_id, client_secret, status, last_sync, last_sync_result"""
    allowed = {"label", "account_id", "host_email", "webhook_secret", "is_active", "client_id", "client_secret", "status", "last_sync", "last_sync_result"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    clauses = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [now, db_id]
    conn = _get_conn()
    conn.execute(
        f"UPDATE zoom_accounts SET {clauses}, updated_at = ? WHERE id = ?",
        values,
    )
    conn.commit()
    log_audit("update", "zoom_account", db_id, f"Updated Zoom account: {','.join(updates.keys())}")
    return True


def update_zoom_account_status(account_id: int, status: str,
                                last_sync: str = None,
                                last_sync_result: str = None,
                                webhook_last_event: str = None,
                                webhook_last_time: str = None) -> bool:
    """Update zoom_account runtime status fields."""
    conn = _get_conn()
    sets = ["status = ?"]
    vals = [status]
    if last_sync is not None:
        sets.append("last_sync = ?")
        vals.append(last_sync)
    if last_sync_result is not None:
        sets.append("last_sync_result = ?")
        vals.append(last_sync_result)
    if webhook_last_event is not None:
        sets.append("webhook_last_event = ?")
        vals.append(webhook_last_event)
    if webhook_last_time is not None:
        sets.append("webhook_last_time = ?")
        vals.append(webhook_last_time)
    vals.append(account_id)
    conn.execute(f"UPDATE zoom_accounts SET {', '.join(sets)} WHERE id = ?", vals)
    conn.commit()
    return True


# ── Meeting CRUD ────────────────────────────────────────────────────────

def create_meeting(tenant_id: str, account_id: int, meeting_id: str,
                   label: str = "", meeting_type: str = "pmi") -> int:
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO monitored_meetings (tenant_id, account_id, meeting_id, label, meeting_type, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, account_id, meeting_id, label, meeting_type, now),
    )
    conn.commit()
    log_audit("create", "meeting", cur.lastrowid, f"Created meeting: {meeting_id}")
    return cur.lastrowid


def get_monitored_meetings_for_account(account_db_id: int) -> list[dict]:
    """获取指定 Zoom 账号下的监控会议"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM monitored_meetings WHERE account_id = ? AND is_active = 1",
        (account_db_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def get_all_active_monitored_meetings() -> list[dict]:
    """获取所有激活的监控会议（含 account_id 和 tenant_id）"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT mm.*, za.client_id, za.account_id AS zoom_account_id, "
        "       za.client_secret, za.host_email, za.tenant_id "
        "FROM monitored_meetings mm "
        "JOIN zoom_accounts za ON za.id = mm.account_id "
        "JOIN tenants t ON t.id = mm.tenant_id "
        "WHERE mm.is_active = 1 AND za.is_active = 1 AND t.is_active = 1"
    ).fetchall()
    return [dict(r) for r in rows]


def get_meetings(tenant_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM monitored_meetings WHERE tenant_id = ? ORDER BY created_at DESC",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def delete_meeting(meeting_id_db: int) -> bool:
    conn = _get_conn()
    conn.execute("DELETE FROM monitored_meetings WHERE id = ?", (meeting_id_db,))
    conn.commit()
    log_audit("delete", "meeting", meeting_id_db, "Deleted meeting")
    return True


# ── Channel CRUD ────────────────────────────────────────────────────────

def create_tenant_channel(tenant_id: str, chat_id: str, label: str = "",
                          is_group: bool = False,
                          bot_token: str = "",
                          bot_username: str = "") -> int:
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO tenant_channels (tenant_id, chat_id, label, is_group, bot_token, bot_username, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (tenant_id, chat_id, label, 1 if is_group else 0, bot_token, bot_username, now),
    )
    conn.commit()
    log_audit("create", "channel", cur.lastrowid, f"Created channel: {chat_id}")
    return cur.lastrowid


def get_tenant_channels(tenant_id: str) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tenant_channels WHERE tenant_id = ? ORDER BY created_at DESC",
        (tenant_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def toggle_tenant_channel(channel_id: int) -> bool:
    conn = _get_conn()
    row = conn.execute("SELECT is_enabled FROM tenant_channels WHERE id = ?", (channel_id,)).fetchone()
    if not row:
        return False
    new_val = 0 if row[0] else 1
    conn.execute("UPDATE tenant_channels SET is_enabled = ? WHERE id = ?", (new_val, channel_id))
    conn.commit()
    return True


def delete_tenant_channel(channel_id: int) -> bool:
    conn = _get_conn()
    conn.execute("DELETE FROM tenant_channels WHERE id = ?", (channel_id,))
    conn.commit()
    return True


def get_tenant_id_by_zoom_account(zoom_account_id: str) -> str | None:
    """通过 Zoom account_id 反向查找所属 tenant_id。

    用于 Webhook 事件路由：当 Zoom 发送 webhook 时，
    通过 payload 中的 account_id 确定该事件属于哪个租户。
    """
    conn = _get_conn()
    row = conn.execute(
        "SELECT tenant_id FROM zoom_accounts WHERE account_id = ? LIMIT 1",
        (zoom_account_id,),
    ).fetchone()
    return row[0] if row else None


# ── P2 会议中心 ──────────────────────────────────────────────────────

def get_live_meetings(tenant_id: str) -> list[dict]:
    """获取当前正在进行的会议（通过 zoom_participants 的 join/leave 状态推断）"""
    conn = _get_conn()
    from datetime import datetime, timezone, timedelta
    now_utc = datetime.now(timezone.utc)
    today_start = now_utc.strftime("%Y-%m-%d")
    
    rows = conn.execute(
        "SELECT meeting_id, name, action, action_time "
        "FROM zoom_participants "
        "WHERE tenant_id = ? AND action_time >= ? "
        "ORDER BY meeting_id, action_time DESC",
        (tenant_id, today_start),
    ).fetchall()
    
    meetings_map = {}
    for r in rows:
        mid = r["meeting_id"]
        if mid not in meetings_map:
            meetings_map[mid] = {"meeting_id": mid, "participants": set(), "online_count": 0, "last_activity": r["action_time"]}
        meetings_map[mid]["participants"].add(r["name"])
        # 5 分钟内活跃视为在线
        if r["action"] in ("enter", "joined") and r["action_time"] > (now_utc - timedelta(minutes=5)).isoformat():
            meetings_map[mid]["online_count"] += 1
        meetings_map[mid]["last_activity"] = max(meetings_map[mid]["last_activity"], r["action_time"])
    
    for mid in meetings_map:
        topic = conn.execute(
            "SELECT topic FROM meeting_topics WHERE meeting_id = ? LIMIT 1",
            (mid,),
        ).fetchone()
        meetings_map[mid]["topic"] = topic[0] if topic else mid
        meetings_map[mid]["participant_count"] = len(meetings_map[mid]["participants"])
        del meetings_map[mid]["participants"]
    
    return sorted(meetings_map.values(), key=lambda m: m["last_activity"], reverse=True)


def get_meeting_history(tenant_id: str, limit: int = 50, offset: int = 0) -> tuple:
    """获取历史会议列表（按 meeting_id 聚合）"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT meeting_id, name, action, action_time "
        "FROM zoom_participants "
        "WHERE tenant_id = ? "
        "ORDER BY meeting_id, action_time",
        (tenant_id,),
    ).fetchall()
    
    meetings = {}
    for r in rows:
        mid = r["meeting_id"]
        if mid not in meetings:
            meetings[mid] = {"meeting_id": mid, "first_event": r["action_time"], "last_event": r["action_time"], "participant_names": set(), "total_events": 0}
        meetings[mid]["participant_names"].add(r["name"])
        meetings[mid]["total_events"] += 1
        if r["action_time"] < meetings[mid]["first_event"]: meetings[mid]["first_event"] = r["action_time"]
        if r["action_time"] > meetings[mid]["last_event"]: meetings[mid]["last_event"] = r["action_time"]
    
    for mid in meetings:
        topic = conn.execute("SELECT topic FROM meeting_topics WHERE meeting_id = ? LIMIT 1", (mid,)).fetchone()
        meetings[mid]["topic"] = topic[0] if topic else mid
        meetings[mid]["participant_count"] = len(meetings[mid]["participant_names"])
        del meetings[mid]["participant_names"]
        try:
            f = datetime.fromisoformat(meetings[mid]["first_event"].replace("Z", "+00:00"))
            l = datetime.fromisoformat(meetings[mid]["last_event"].replace("Z", "+00:00"))
            dur = int((l - f).total_seconds())
            meetings[mid]["duration_seconds"] = dur
            meetings[mid]["duration_display"] = _fmt_dur(dur)
        except:
            meetings[mid]["duration_seconds"] = 0
            meetings[mid]["duration_display"] = "—"
    
    sorted_list = sorted(meetings.values(), key=lambda m: m["last_event"], reverse=True)
    total = len(sorted_list)
    page = sorted_list[offset:offset + limit]
    for m in page:
        m["first_event_display"] = _myt_short(m["first_event"])
        m["last_event_display"] = _myt_short(m["last_event"])
    return page, total


def get_sharing_records(tenant_id: str, limit: int = 50, today_only: bool = False) -> list[dict]:
    """获取共享屏幕历史记录
    today_only=True: 只返回今天 MYT 的数据
    自动过滤：负数时长、end<start、24h+异常、同人同meeting同时段去重
    """
    conn = _get_conn()
    from datetime import datetime, timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    now_myt = datetime.now(timezone.utc).astimezone(MYT)
    today_start_utc = (now_myt - timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end_utc = today_start_utc + timedelta(days=1)
    stale_threshold_utc = (now_myt - timedelta(hours=6)).astimezone(timezone.utc)
    
    if today_only:
        rows = conn.execute(
            "SELECT * FROM sharing_live WHERE tenant_id = ? AND start_time >= ? AND start_time < ? ORDER BY start_time DESC LIMIT ?",
            (tenant_id, today_start_utc.isoformat(), today_end_utc.isoformat(), limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM sharing_live WHERE tenant_id = ? ORDER BY start_time DESC LIMIT ?",
            (tenant_id, limit),
        ).fetchall()
    
    seen = set()
    result = []
    meta = {"invalid_count": 0, "stale_active_count": 0, "stale_active_items": []}
    for r in rows:
        d = dict(r)
        
        # 过滤负数时长/end<start
        if d.get("end_time") and d.get("start_time"):
            try:
                s = d["start_time"].replace("Z", "+00:00")
                e = d["end_time"].replace("Z", "+00:00")
                st = datetime.fromisoformat(s)
                et = datetime.fromisoformat(e)
                if et.tzinfo is None: et = et.replace(tzinfo=timezone.utc)
                if st.tzinfo is None: st = st.replace(tzinfo=timezone.utc)
                dur_sec = int((et - st).total_seconds())
                # 负数 / end<start / 超过24h
                if dur_sec < 0 or et < st or dur_sec > 86400:
                    meta["invalid_count"] += 1
                    continue
            except:
                meta["invalid_count"] += 1
                continue
        
        # 去重：meeting_id + user_name + start_time + end_time（空值保护）
        dedup_key = f"{d.get('meeting_id','')}|{d.get('user_name','')}|{(d.get('start_time') or '')[:16]}|{(d.get('end_time') or '')[:16]}"
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        
        # 标记 stale active：is_active=1 且 start_time 超过 6h
        is_stale = False
        if d.get("is_active") == 1 and d.get("start_time"):
            try:
                st_dt = datetime.fromisoformat(d["start_time"].replace("Z", "+00:00"))
                if st_dt.tzinfo is None: st_dt = st_dt.replace(tzinfo=timezone.utc)
                if st_dt < stale_threshold_utc:
                    is_stale = True
            except:
                pass
        if is_stale:
            meta["stale_active_count"] += 1
            meta["stale_active_items"].append({
                "user_name": d.get("user_name"),
                "start_time": _myt_short(d.get("start_time", "")),
                "meeting_id": d.get("meeting_id"),
            })
            continue  # 不在"当前共享"中展示
        
        # 时长计算
        try:
            dur_sec = int((et - st).total_seconds()) if d.get("end_time") and d.get("start_time") else 0
            d["duration"] = _fmt_dur(dur_sec)
            d["duration_seconds"] = dur_sec
        except:
            d["duration"] = "—"
            d["duration_seconds"] = 0
        
        d["start_time_display"] = _myt_short(d.get("start_time", ""))
        d["end_time_display"] = _myt_short(d.get("end_time", ""))
        result.append(d)
    return result, len(result), meta


# ── 统一的在线计算（Single Source of Truth）────────────────────────────────────


def get_current_online(tenant_id: str | None = None) -> dict:
    """统一的在线人数计算。

    条件（缺一不可）：
    1. 该人最后一条动作是 enter/joined
    2. 该人在 15 分钟内（900 秒）有活动记录（idle_minutes < 15）
    3. 该会议未被 meeting.ended 标记结束

    返回统一结构：
    {
        "online_count": int,
        "online_names": list[str],
        "active_meetings": list[dict],
        "source": "webhook_with_idle",
    }
    """
    STALE_WINDOW_MINUTES = 90
    IDLE_TIMEOUT_SECONDS = 900  # 15 分钟

    conn = _get_conn()

    # ── 读取 meeting.ended 事件的 meeting_id 列表 ──
    import json
    ended_meetings: set[str] = set()
    ended_rows = conn.execute(
        "SELECT payload FROM zoom_events "
        "WHERE event_type='meeting.ended' "
        "AND created_at >= datetime('now', '-24 hours')"
    ).fetchall()
    for (payload_str,) in ended_rows:
        try:
            p = json.loads(payload_str)
            mid = str(p.get("payload", {}).get("object", {}).get("id", ""))
            if mid:
                ended_meetings.add(mid)
        except (json.JSONDecodeError, AttributeError):
            continue

    # ── 读取 90 分钟内参与者动作 ──
    if tenant_id:
        rows = conn.execute(
            "SELECT name, action, action_time, meeting_id "
            "FROM zoom_participants "
            "WHERE tenant_id=? AND action_time >= datetime('now', ? || ' minutes') "
            "AND action IN ('enter', 'leave', 'joined', 'left') "
            "ORDER BY name, action_time DESC",
            (tenant_id, f'-{STALE_WINDOW_MINUTES}'),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT name, action, action_time, meeting_id "
            "FROM zoom_participants "
            "WHERE action_time >= datetime('now', ? || ' minutes') "
            "AND action IN ('enter', 'leave', 'joined', 'left') "
            "ORDER BY name, action_time DESC",
            (f'-{STALE_WINDOW_MINUTES}',),
        ).fetchall()

    # ── 逐人判断 ──
    from datetime import datetime, timezone
    now_utc = datetime.now(timezone.utc)
    online_names: list[str] = []
    seen: set[str] = set()
    for r in rows:
        name = r["name"]
        if name in seen:
            continue
        seen.add(name)
        action = r["action"]
        meeting_id = r["meeting_id"]

        # 条件3：会议已结束 → 跳过
        if meeting_id in ended_meetings:
            continue

        try:
            last_dt = datetime.fromisoformat(r["action_time"])
        except Exception:
            continue

        # 条件1：最后动作是 enter/joined
        # 条件2：15 分钟内有活动
        if action in ("enter", "joined") and (now_utc - last_dt).total_seconds() < IDLE_TIMEOUT_SECONDS:
            online_names.append(name)

    online_names.sort()
    online_count = len(online_names)

    return {
        "online_count": online_count,
        "online_names": online_names,
        "active_meetings": [],
        "source": "webhook_with_idle",
    }