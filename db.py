"""
db.py — SQLite 数据库操作
Schema：
  - zoom_events:     原始 Webhook 事件
  - zoom_participants:参会记录（进出）
  - seen_emails:     邮箱去重
  - alerts:          告警日志
  - settings:        持久化设置（命令控制）
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
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


def init_db():
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

def normalize_identity_name(name: str) -> str:
    return " ".join((name or "").strip().lower().split())


def check_new_email(email: str, name: str, now: datetime) -> bool:
    """返回 True 表示新人，False 表示已见过

    有 email 时按 email 去重；无 email 时 fallback 到规范化姓名。
    """
    email = (email or "").strip().lower()
    name_key = normalize_identity_name(name)

    conn = _get_conn()

    if email:
        row = conn.execute(
            "SELECT 1 FROM seen_emails WHERE email = ? LIMIT 1", (email,)
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO seen_emails (email, name, first_seen, last_seen) VALUES (?, ?, ?, ?)",
                (email, name, now.isoformat(), now.isoformat()),
            )
            conn.commit()
            return True
        conn.execute(
            "UPDATE seen_emails SET name = ?, last_seen = ? WHERE email = ?",
            (name, now.isoformat(), email),
        )
        conn.commit()
        return False

    # fallback：无 email 时按姓名识别
    if not name_key:
        return False

    pseudo_email = f"name:{name_key}"
    row = conn.execute(
        "SELECT 1 FROM seen_emails WHERE email = ? LIMIT 1", (pseudo_email,)
    ).fetchone()

    if not row:
        conn.execute(
            "INSERT INTO seen_emails (email, name, first_seen, last_seen) VALUES (?, ?, ?, ?)",
            (pseudo_email, name, now.isoformat(), now.isoformat()),
        )
        conn.commit()
        return True

    conn.execute(
        "UPDATE seen_emails SET name = ?, last_seen = ? WHERE email = ?",
        (name, now.isoformat(), pseudo_email),
    )
    conn.commit()
    return False


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
