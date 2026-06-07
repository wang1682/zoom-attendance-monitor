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

    # seed default telegram alert rules
    if not readonly: seed_telegram_rules()
    if not readonly: seed_member_groups()

    # seed default telegram channel
    _seed_default_telegram_channel()


# ── zoom_events ──────────────────────────────────────────────────────────────

def save_webhook_event(event_type: str, payload: dict) -> int:
    import json
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO zoom_events (event_type, payload) VALUES (?, ?)",
        (event_type, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    return cur.lastrowid


def get_recent_events(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM zoom_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


# ── zoom_participants ────────────────────────────────────────────────────────

def save_participant(
    meeting_id: str, name: str, email: str,
    action: str, action_time: datetime,
    source: str = "poll",
) -> int:
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO zoom_participants (meeting_id, name, email, action, action_time, source) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (meeting_id, name, email, action, action_time.isoformat(), source),
    )
    conn.commit()
    return cur.lastrowid


def get_today_participants(limit: int = 200) -> list[dict]:
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM zoom_participants WHERE action_time >= ? ORDER BY action_time DESC LIMIT ?",
        (today, limit),
    ).fetchall()
    return [dict(r) for r in rows]


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


def get_recent_alerts(limit: int = 50, alert_type: str = None) -> list[dict]:
    conn = _get_conn()
    if alert_type:
        rows = conn.execute(
            "SELECT * FROM alerts WHERE alert_type = ? ORDER BY id DESC LIMIT ?",
            (alert_type, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
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



_display_cache = {"mapping": {}, "ts": 0}

def resolve_display_name(raw_name: str) -> dict:
    """返回 {display_name, count_enabled, raw_name}"""
    import time, re
    now = time.time()
    if not _display_cache["mapping"] or now - _display_cache["ts"] > 30:
        conn = _get_conn()
        rows = conn.execute("SELECT raw_name, display_name, match_key, count_enabled, aliases FROM member_display").fetchall()
        _display_cache["mapping"] = {
            r[0]: {"display": r[1], "key": r[2], "enabled": bool(r[3]), "aliases": json.loads(r[4] or "[]")}
            for r in rows
        }
        _display_cache["ts"] = now
    
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

    # 4. No match, return as-is
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
        for key in ("title", "enabled", "target_chat_id", "target_channel_id", "cooldown_seconds",
                     "quiet_enabled", "quiet_start", "quiet_end"):
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
            "(event_type, title, enabled, target_chat_id, target_channel_id, cooldown_seconds, "
            " quiet_enabled, quiet_start, quiet_end, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_type,
                data.get("title", ""),
                data.get("enabled", 1),
                data.get("target_chat_id", ""),
                data.get("target_channel_id", None),
                data.get("cooldown_seconds", 60),
                data.get("quiet_enabled", 0),
                data.get("quiet_start", "00:00"),
                data.get("quiet_end", "08:00"),
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
        for key in ("chat_id", "enabled", "is_default", "notes"):
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
            "INSERT INTO telegram_channels (name, chat_id, enabled, is_default, notes, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                name,
                chat_id,
                data.get("enabled", 1),
                data.get("is_default", 0),
                data.get("notes", ""),
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


def get_member_group(member_name: str) -> str | None:
    if not member_name:
        return None
    conn = _get_conn()
    name = member_name.strip().lower().replace(" ", "")
    # 优先从 member_display.group_id 读取（新方式）
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


def get_all_groups() -> list[dict]:
    conn = _get_conn()
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
