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

ROLES = ["super_admin", "admin", "tenant_admin", "user", "viewer"]
ROLE_HIERARCHY = {
    "super_admin": 4,
    "admin": 3,
    "tenant_admin": 2,
    "user": 1,
    "viewer": 0,
}

def role_ge(user_role: str, required_role: str) -> bool:
    """Check if user_role >= required_role in hierarchy"""
    return ROLE_HIERARCHY.get(user_role, 0) >= ROLE_HIERARCHY.get(required_role, 0)

def to_myt_str(dt_str: str) -> str:
    """将 UTC ISO/Datetime 字符串转 MYT MM-DD HH:mm:ss"""
    if not dt_str:
        return ""
    try:
        s = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        return dt.astimezone(TZ_MYT).strftime("%m-%d %H:%M:%S")
    except Exception:
        return dt_str[:16]


def get_zoom_account(tenant_id: str) -> dict | None:
    """从 zoom_accounts 表查当前租户的 Zoom 账号"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT id, tenant_id, account_id, client_id, client_secret, host_email "
        "FROM zoom_accounts WHERE tenant_id = ? AND is_active = 1 "
        "ORDER BY id DESC LIMIT 1",
        (tenant_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "tenant_id": row[1],
        "account_id": row[2],
        "client_id": row[3],
        "client_secret": row[4],
        "host_email": row[5],
    }


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
        "  tenant_id TEXT NOT NULL DEFAULT 'default',"
        "  event_type TEXT NOT NULL,"
        "  title TEXT NOT NULL DEFAULT '',"
        "  enabled INTEGER NOT NULL DEFAULT 1,"
        "  target_chat_id TEXT DEFAULT '',"
        "  target_channel_id INTEGER DEFAULT NULL,"
        "  cooldown_seconds INTEGER DEFAULT 60,"
        "  quiet_enabled INTEGER DEFAULT 0,"
        "  quiet_start TEXT DEFAULT '00:00',"
        "  quiet_end TEXT DEFAULT '08:00',"
        "  created_at TEXT,"
        "  updated_at TEXT,"
        "  UNIQUE(tenant_id, event_type)"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_telegram_rules_tenant_event ON telegram_alert_rules(tenant_id, event_type)",

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

        "CREATE TABLE IF NOT EXISTS shift_assignments ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  tenant_id TEXT NOT NULL DEFAULT 'default',"
        "  member_name TEXT NOT NULL,"
        "  shift_name TEXT NOT NULL,"
        "  shift_date TEXT NOT NULL,"
        "  shift_start TEXT NOT NULL,"
        "  shift_end TEXT NOT NULL,"
        "  created_by INTEGER,"
        "  created_at TEXT,"
        "  UNIQUE(tenant_id, member_name, shift_date)"
        ")",

        "CREATE INDEX IF NOT EXISTS idx_shift_asgn_date ON shift_assignments(shift_date)",
        "CREATE INDEX IF NOT EXISTS idx_shift_asgn_tenant ON shift_assignments(tenant_id)",

        # ── identity stability analysis ──
        "CREATE TABLE IF NOT EXISTS member_identity_stats ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  tenant_id TEXT NOT NULL,"
        "  member_key TEXT NOT NULL,"
        "  raw_name TEXT NOT NULL,"
        "  raw_name_count INTEGER NOT NULL DEFAULT 1,"
        "  last_raw_name_seen TEXT,"
        "  public_ip TEXT,"
        "  ip_count INTEGER NOT NULL DEFAULT 1,"
        "  last_ip_seen TEXT,"
        "  user_id TEXT,"
        "  participant_uuid TEXT,"
        "  email TEXT,"
        "  session_date TEXT,"
        "  join_time TEXT,"
        "  leave_time TEXT,"
        "  duration_minutes REAL,"
        "  updated_at TEXT DEFAULT (datetime('now'))"
        ")",
        "CREATE INDEX IF NOT EXISTS idx_mis_tenant_member ON member_identity_stats(tenant_id, member_key)",
        "CREATE INDEX IF NOT EXISTS idx_mis_tenant_date ON member_identity_stats(tenant_id, session_date)",
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
    # ⚠️ 只填空（group_id IS NULL），不覆盖已有分组，防止 migration 双写回滚
    try:
        rows = conn.execute(
            "SELECT mgm.group_id, mgm.member_name, md.raw_name, md.tenant_id "
            "FROM member_group_members mgm "
            "JOIN member_display md ON md.raw_name = mgm.member_name "
            "JOIN member_groups mg ON mg.id = mgm.group_id "
            "WHERE md.tenant_id == mg.tenant_id AND md.group_id IS NULL"
        ).fetchall()
        for gid, mname, raw_name, md_tenant in rows:
            conn.execute(
                "UPDATE member_display SET group_id = ? WHERE raw_name = ? AND group_id IS NULL",
                (gid, raw_name),
            )
        # For members not yet in member_display, create placeholder entries
        rows2 = conn.execute(
            "SELECT mgm.group_id, mgm.member_name, mg.tenant_id "
            "FROM member_group_members mgm "
            "JOIN member_groups mg ON mg.id = mgm.group_id "
            "LEFT JOIN member_display md ON md.raw_name = mgm.member_name "
            "WHERE md.id IS NULL"
        ).fetchall()
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for gid, mname, mgm_tenant in rows2:
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

    # seed default telegram alert rules for all tenants
    if not readonly:
        seed_telegram_rules()
        try:
            for t in get_all_tenants():
                if t["id"] != "default":
                    seed_telegram_rules(t["id"])
        except Exception:
            pass  # 表可能还不存在
    if not readonly: seed_member_groups()

    # seed default telegram channel
    _seed_default_telegram_channel()

    # multi-tenant migrations
    if not readonly:
        run_mt_migrations()

    # migrate: add Telegram 2FA columns to users
    for col_sql in [
        "ALTER TABLE users ADD COLUMN telegram_chat_id TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN telegram_2fa_enabled INTEGER DEFAULT 0",
        "ALTER TABLE users ADD COLUMN telegram_2fa_verified_at TEXT",
        "ALTER TABLE users ADD COLUMN twofa_secret TEXT DEFAULT ''",
        "ALTER TABLE users ADD COLUMN twofa_backup_codes TEXT DEFAULT ''",
    ]:
        try:
            conn.execute(col_sql)
        except Exception:
            pass

    # create login_attempts table (for rate limiting)
    conn.execute("CREATE TABLE IF NOT EXISTS login_attempts ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  ip TEXT NOT NULL,"
        "  username TEXT NOT NULL DEFAULT '',"
        "  failed_count INTEGER NOT NULL DEFAULT 1,"
        "  locked_until TEXT,"
        "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_login_attempts_ip ON login_attempts(ip)")

    # create security_audit_logs table
    conn.execute("CREATE TABLE IF NOT EXISTS security_audit_logs ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  user_id INTEGER,"
        "  username TEXT DEFAULT '',"
        "  tenant_id TEXT DEFAULT '',"
        "  action TEXT NOT NULL,"
        "  ip TEXT DEFAULT '',"
        "  user_agent TEXT DEFAULT '',"
        "  result TEXT DEFAULT 'success',"
        "  details TEXT DEFAULT '',"
        "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_security_audit_action ON security_audit_logs(action)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_security_audit_created ON security_audit_logs(created_at)")

    # ip_cache table for geo-location
    conn.execute("CREATE TABLE IF NOT EXISTS ip_cache ("
        "  ip TEXT PRIMARY KEY,"
        "  location TEXT NOT NULL DEFAULT '',"
        "  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")")

    # ── official_attendance_sessions (Zoom 官方 Attendance CSV 导入) ──
    conn.execute("CREATE TABLE IF NOT EXISTS official_attendance_sessions ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  tenant_id TEXT NOT NULL,"
        "  meeting_id TEXT,"
        "  topic TEXT,"
        "  host_name TEXT,"
        "  host_email TEXT,"
        "  meeting_start_time TEXT,"
        "  meeting_end_time TEXT,"
        "  participant_name TEXT NOT NULL,"
        "  email TEXT,"
        "  join_time TEXT NOT NULL,"
        "  leave_time TEXT NOT NULL,"
        "  duration_minutes REAL,"
        "  guest TEXT,"
        "  in_waiting_room TEXT,"
        "  source_file TEXT,"
        "  imported_at TEXT DEFAULT (datetime('now'))"
        ")")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oas_tenant_name ON official_attendance_sessions(tenant_id, participant_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oas_tenant_date ON official_attendance_sessions(tenant_id, join_time)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oas_meeting   ON official_attendance_sessions(meeting_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_oas_import    ON official_attendance_sessions(source_file)")


# ── official_attendance_sessions 导入 & 查询 ──
# Zoom 官方 Attendance CSV → official_attendance_sessions 表
# 不与 zoom_participants 混用

def import_official_attendance_csv(
    csv_path: str,
    tenant_id: str = "default",
    source_file: str = "",
) -> dict:
    """导入 Zoom 官方 Attendance CSV 到 official_attendance_sessions。

    CSV 格式：UTF-8 BOM，中文列名，Zoom 官方 Attendance Report。

    列名（按顺序）：
        主题,类型,ID,主持人名称,主持人电子邮件,开始时间,结束时间,
        参会者,持续时间（分钟）,参会者总分钟数,
        名称（原名）,电子邮件,加入时间,离开时间,持续时间（分钟）,
        访客,在等候室中

    注意：有重复列名 "持续时间（分钟）"（会议级 + 参与者级），
    我们用 raw header 定位最后一个（参与者级）。

    时间格式：2026/06/21 07:05:20 AM（UTC）
    """
    import csv

    conn = _get_conn()
    rows_raw = []

    # 先读 header 行 - 找参与者级持续时间列的位置
    with open(csv_path, encoding="utf-8-sig") as f:
        raw_header = f.readline().strip().split(",")

    # Python csv.DictReader 遇到重复列名会保留第一个，丢弃后续同名列
    # 所以需要用原始 header 定位参与者级 "持续时间（分钟）"
    dur_key = "持续时间（分钟）"
    dur_indices = [i for i, h in enumerate(raw_header) if dur_key in h]
    participant_dur_idx = dur_indices[-1] if dur_indices else -1

    # ── 用 csv.reader 而非 DictReader ──
    # 因为列名有重复（"持续时间（分钟）" 出现 2 次），DictReader 会丢失第二列
    # 改用 reader + dict zipped header，按最后列索引取值
    with open(csv_path, encoding="utf-8-sig") as f:
        reader = csv.reader(f)
        csv_header = next(reader)
        for row in reader:
            rows_raw.append(row)

    if not rows_raw:
        return {"ok": False, "imported": 0, "errors": ["CSV 为空或无数据行"]}

    # 建立列名→索引映射（保留最后出现的位置，解决重复列名问题）
    col_index = {}
    for ci, name in enumerate(csv_header):
        col_index[name] = ci  # 后出现的覆盖前面的

    # 帮助函数：按列名取值
    def _val_or_none(row, name):
        idx = col_index.get(name)
        if idx is not None and idx < len(row):
            return row[idx].strip()
        return ""

    imported = 0
    skipped = 0
    errors = []

    for i, row in enumerate(rows_raw):
        try:
            # 基础字段
            meeting_id = _val_or_none(row, "ID").replace(" ", "")
            topic = _val_or_none(row, "主题")
            host_name = _val_or_none(row, "主持人名称")
            host_email = _val_or_none(row, "主持人电子邮件")
            meeting_start = _val_or_none(row, "开始时间")
            meeting_end = _val_or_none(row, "结束时间")
            participant_name = _val_or_none(row, "名称（原名）")
            email = _val_or_none(row, "电子邮件")
            join_time = _val_or_none(row, "加入时间")
            leave_time = _val_or_none(row, "离开时间")
            guest = _val_or_none(row, "访客")
            in_waiting = _val_or_none(row, "在等候室中")

            # 参与者级持续时间 — 按列名取（index 对应最后一个 duration 列）
            duration_minutes = None
            dur_str = _val_or_none(row, "持续时间（分钟）")
            if dur_str:
                try:
                    duration_minutes = float(dur_str)
                except (ValueError, TypeError):
                    pass

            # 时间标准化: "2026/06/21 07:05:20 AM" → ISO UTC isoformat
            def _fmt_zoom_time(t: str) -> str:
                if not t:
                    return ""
                t = t.strip()
                try:
                    dt = datetime.strptime(t, "%Y/%m/%d %I:%M:%S %p")
                    return dt.isoformat()
                except ValueError:
                    return t

            join_dt = _fmt_zoom_time(join_time)
            leave_dt = _fmt_zoom_time(leave_time)

            if not participant_name or not join_dt:
                skipped += 1
                continue

            conn.execute(
                """INSERT INTO official_attendance_sessions
                   (tenant_id, meeting_id, topic, host_name, host_email,
                    meeting_start_time, meeting_end_time,
                    participant_name, email, join_time, leave_time,
                    duration_minutes, guest, in_waiting_room, source_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    tenant_id, meeting_id, topic, host_name, host_email,
                    meeting_start, meeting_end,
                    participant_name, email, join_dt, leave_dt,
                    duration_minutes, guest, in_waiting, source_file or csv_path,
                ),
            )
            imported += 1
        except Exception as e:
            errors.append(f"row {i + 2}: {e}")
            skipped += 1

    conn.commit()
    return {"ok": True, "imported": imported, "skipped": skipped, "errors": errors}


def get_official_sessions_for_member(
    tenant_id: str,
    participant_name: str,
    date_from: str = None,
    date_to: str = None,
    limit: int = 500,
) -> list[dict]:
    """查询 official_attendance_sessions 中某个成员的数据（大小写不敏感）。"""
    conn = _get_conn()
    conditions = ["tenant_id = ?", "LOWER(participant_name) = LOWER(?)"]
    params = [tenant_id, participant_name]
    if date_from:
        conditions.append("join_time >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("join_time < ?")
        params.append(date_to)
    sql = f"SELECT * FROM official_attendance_sessions WHERE {' AND '.join(conditions)} ORDER BY join_time ASC LIMIT ?"
    params.append(limit)
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def get_official_session_daily_summary(
    tenant_id: str,
    participant_name: str,
    limit: int = 60,
) -> dict:
    """按天汇总某个成员的官方 Session，返回每日聚合 + sessions 明细。

    返回: {
        "member": name,
        "total_days": N,        # 出勤天数
        "total_duration": N,    # 累计在线分钟数
        "daily": [
            {
                "date": "2026-06-22",
                "date_display": "06-22",
                "session_count": N,
                "duration_minutes": N,
                "duration_display": "9h12m",
                "first_join": iso,
                "last_leave": iso,
                "sessions": [{...}]
            },
            ...
        ]
    }
    """
    conn = _get_conn()
    # 先查该成员所有 session，关联 member_display 做 canonical 匹配
    rows = get_official_sessions_for_member(tenant_id, participant_name, limit=10000)
    if not rows:
        return {"member": participant_name, "total_days": 0, "total_duration": 0, "daily": []}

    import re
    # 按 MYT 日期分组（join_time 是 UTC 时间）
    from collections import OrderedDict
    daily_map = OrderedDict()

    for r in rows:
        jt = r.get("join_time")
        if not jt:
            continue
        lt = r.get("leave_time") or jt
        dur_min = r.get("duration_minutes", 0) or 0

        # UTC → MYT 转日期
        # join_time 格式如 "2026-06-22T07:24:00+00:00" 或 "2026-06-22 07:24:00"
        jt_str = str(jt).replace("T", " ").replace("Z", "").replace("+00:00", "").replace("+08:00", "")
        if "." in jt_str:
            jt_str = jt_str.split(".")[0]
        try:
            from datetime import datetime, timedelta
            utc_dt = datetime.strptime(jt_str, "%Y-%m-%d %H:%M:%S")
            myt_dt = utc_dt + timedelta(hours=8)
            date_key = myt_dt.strftime("%Y-%m-%d")
            date_display = myt_dt.strftime("%m-%d")
        except (ValueError, IndexError):
            date_key = jt_str[:10]
            date_display = date_key[5:] if len(date_key) >= 10 else date_key

        if date_key not in daily_map:
            daily_map[date_key] = {
                "date": date_key,
                "date_display": date_display,
                "sessions": [],
                "duration_minutes": 0,
            }
        daily_map[date_key]["sessions"].append({
            "meeting_id": r.get("meeting_id", ""),
            "topic": r.get("topic", ""),
            "join_time": r.get("join_time"),
            "leave_time": r.get("leave_time"),
            "duration_minutes": int(dur_min),
            "duration_display": _fmt_dur_min(int(dur_min)),
        })
        daily_map[date_key]["duration_minutes"] += int(dur_min)

    # 构建 daily 列表（逆序，最新在前）
    daily = []
    for dk in reversed(list(daily_map.keys())):
        d = daily_map[dk]
        d["duration_display"] = _fmt_dur_min(d["duration_minutes"])
        d["session_count"] = len(d["sessions"])
        if d["sessions"]:
            d["first_join"] = d["sessions"][0]["join_time"]
            d["last_leave"] = d["sessions"][-1]["leave_time"]
        daily.append(d)

        # 只返回最近 limit 天
        if len(daily) >= limit:
            break

    total_days = len(daily)
    total_duration = sum(d["duration_minutes"] for d in daily)

    return {
        "member": participant_name,
        "total_days": total_days,
        "total_duration": total_duration,
        "total_duration_display": _fmt_dur_min(total_duration),
        "avg_daily_display": _fmt_dur_min(total_duration // total_days) if total_days > 0 else "0m",
        "last_active_display": daily[0]["date_display"] if daily else "—",
        "daily": daily,
    }


def _fmt_dur_min(total_min: int) -> str:
    """分钟转可读时长"""
    total_min = total_min or 0
    if total_min >= 1440:
        d = total_min // 1440
        h = (total_min % 1440) // 60
        m = total_min % 60
        return f"{d}d {h}h {m}m" if m else f"{d}d {h}h"
    elif total_min >= 60:
        h = total_min // 60
        m = total_min % 60
        return f"{h}h {m}m" if m else f"{h}h"
    else:
        return f"{total_min}m"


def normalize_member_name(name: str) -> tuple:
    """归一化成员名称

    返回 (display_name, member_key)
    display_name: 保留格式，仅清理身份括号
    member_key:   完全压缩，用于聚合去重

    规则：
    1. 去掉 Host 标记：(Host)（Host）
    2. 去掉括号内容包含身份关键词的括号
       （关键词：DC、值班号、duty、host、admin、room、Duty、Room）
    3. 去掉前后空格
    4. 转小写
    5. 全角半角统一（〜→~）
    6. 连续空格压缩
    7. 重复单词去重（"patheon patheon" → "patheon"，至少 3 字符防误伤）
    8. member_key 额外：去除所有空格
    """
    import re
    # Step 1: 精确去 Host 标记
    name = re.sub(r"[（(]\s*[Hh][Oo][Ss][Tt]\s*[）)]", "", name)
    # Step 2: 去掉括号内容包含身份关键词的括号
    kw_pattern = re.compile(
        r"[（(][^）)]*"
        r"(DC|值班号|duty|host|admin|room)"
        r"[^）)]*[）)]",
        re.IGNORECASE,
    )
    prev = None
    while prev != name:
        prev = name
        name = kw_pattern.sub("", name)
    name = (name or "").strip()
    display_name = name.strip()
    name = name.lower()
    name = name.replace("〜", "~")
    name = re.sub(r"\s{2,}", " ", name)
    # Step 7: 重复单词去重（"patheon patheon" → "patheon"）
    name = re.sub(r"\b(\w{3,})\s+\1\b", r"\1", name)
    # member_key: 去所有空格 + 去纯标点符号（如 ~）
    member_key = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", name)
    return display_name, member_key


def get_matrix(tenant_id, year, month):
    import calendar
    conn = _get_conn()
    _, total_days = calendar.monthrange(year, month)
    ds = f"{year:04d}-{month:02d}-01"
    de = f"{year:04d}-{month:02d}-{total_days:02d}"
    # Build dates list: show up to actual last data day, not the full month
    last_data_date = max(
        conn.execute(
            "SELECT DATE(MAX(join_time)) FROM official_attendance_sessions WHERE tenant_id=?",
            (tenant_id,)
        ).fetchone()[0] or ds,
        conn.execute(
            "SELECT DATE(MAX(action_time)) FROM zoom_participants WHERE tenant_id=?",
            (tenant_id,)
        ).fetchone()[0] or ds
    )
    last_day = int(last_data_date.split("-")[2]) if last_data_date >= ds else total_days
    display_days = last_day
    dates = [f"{month:02d}-{d:02d}" for d in range(1, display_days + 1)]

    # 获取带有 email 的详细记录
    rows = conn.execute(
        "SELECT participant_name, email, DATE(join_time), SUM(duration_minutes) "
        "FROM official_attendance_sessions "
        "WHERE tenant_id=? AND DATE(join_time)>=? AND DATE(join_time)<=? "
        "GROUP BY participant_name, email, DATE(join_time) ORDER BY participant_name",
        (tenant_id, ds, de)
    ).fetchall()

    MIN_ATTENDANCE_MINUTES = 360  # 6 小时

    from collections import OrderedDict

    def make_identity_key(raw_name: str, email: str) -> tuple:
        """返回 (identity_key, fallback_identity_key)
        primary: 有 email 则用 email，无则用 name key
        但 email 聚合仅在同组 name key 收敛时才跨 raw_name 合并
        业务确认的 alias 映射表（仅当规则无法合并时才添加）
        """
        ALIAS = {
            "antheafk": "anthea",
            "harysonharyson": "haryson",
            "crispin": "crispini",
            "dcyoungest": "youngest",
            "dcoceanus": "oceanus",
        }
        name_key = normalize_member_name(raw_name)[1]
        name_key = ALIAS.get(name_key, name_key)
        if email and email.strip():
            email_key = f"{tenant_id}:email:{email.strip().lower()}"
            return (email_key, f"{tenant_id}:name:{name_key}")
        return (f"{tenant_id}:name:{name_key}", None)

    # identity_key -> {dates: set, raw_names: [str], emails: [str], name_counts: {raw_name: int} [, name_keys: set]}
    member_groups = OrderedDict()

    total_raw = 0

    for raw_name, email, d, total_min in rows:
        if total_min is None or total_min < MIN_ATTENDANCE_MINUTES:
            continue
        total_raw += 1

        email_key, fallback_key = make_identity_key(raw_name, email)
        name_key = normalize_member_name(raw_name)[1]

        # 决定用哪个 key
        if email_key is not None and fallback_key is not None:
            # 有 email: 先看这个 email key 是否已有同 name key 的成员
            if email_key in member_groups:
                existing_name_keys = member_groups[email_key].get("name_keys", set())
                # 只有当 name key 收敛到已有 keys 时才合并
                if name_key in existing_name_keys or not existing_name_keys:
                    identity_key = email_key
                else:
                    identity_key = fallback_key
            else:
                identity_key = email_key
        else:
            identity_key = email_key  # 无 email 时 email_key 就是 name key

        if identity_key not in member_groups:
            member_groups[identity_key] = {
                "dates": set(),
                "raw_names": [],
                "emails": set(),
                "name_counts": {},
                "first_canon": None,
                "name_keys": set() if email_key else None,
            }

        # Track name keys
        if email_key and member_groups[identity_key].get("name_keys") is not None:
            member_groups[identity_key]["name_keys"].add(name_key)

        # Track raw names
        stripped = raw_name.strip()
        if stripped not in member_groups[identity_key]["raw_names"]:
            member_groups[identity_key]["raw_names"].append(stripped)
            if member_groups[identity_key]["first_canon"] is None:
                canon, _ = normalize_member_name(stripped)
                member_groups[identity_key]["first_canon"] = canon

        # Track email
        if email and email.strip():
            member_groups[identity_key]["emails"].add(email.strip().lower())

        # Count appearances for this identity (same raw_name may appear multiple days)
        member_groups[identity_key]["name_counts"][stripped] = member_groups[identity_key]["name_counts"].get(stripped, 0) + 1

        # Add date
        day_num = int(d.split("-")[2])
        member_groups[identity_key]["dates"].add(day_num)

    # 用最常出现的 raw_name 作为显示名
    for g in member_groups.values():
        best_name = max(g["name_counts"], key=g["name_counts"].get)
        canon, _ = normalize_member_name(best_name)
        g["display_name"] = canon or g["first_canon"] or best_name

    # 统计
    merged_count = len(member_groups)
    duplicate_group_count = sum(1 for g in member_groups.values() if len(g["raw_names"]) > 1)
    valid_members = merged_count
    raw_name_count = sum(len(g["raw_names"]) for g in member_groups.values())

    # Build members list with sort by total desc → name asc
    members = []
    for identity_key, g in member_groups.items():
        ad = sorted(g["dates"])
        members.append({
            "name": g["display_name"],
            "attendance_dates": ad,
            "total": len(ad),
            "raw_names": g["raw_names"],
            "identity_key": identity_key,
            "emails": sorted(g["emails"]),
        })

    members.sort(key=lambda x: (-x["total"], x["name"]))
    return {
        "year": year,
        "month": month,
        "total_days": total_days,
        "display_days": display_days,
        "last_data_date": last_data_date,
        "dates": dates,
        "members": members,
        # 新增统计
        "stats": {
            "raw_member_count": raw_name_count,  # 原始名称去重数量（去重后的原始名条目）
            "valid_members": valid_members,  # 有效去重成员数
            "duplicate_group_count": duplicate_group_count,  # 包含多个原始名的组数
            "merged_member_count": merged_count,  # 合并后的唯一成员数
            "duplicate_total": sum(len(g["raw_names"]) - 1 for g in member_groups.values() if len(g["raw_names"]) > 1),  # 累计重复名称条数
        },
    }


def get_official_session_summary(
    tenant_id: str,
    meeting_id: str = None,
    date_from: str = None,
    date_to: str = None,
) -> list[dict]:
    """从 official_attendance_sessions 获取聚合摘要。

    返回: [{participant_name, email, session_count, total_duration_minutes, first_join, last_leave}]
    """
    conn = _get_conn()
    conditions = ["tenant_id = ?"]
    params = [tenant_id]
    if meeting_id:
        conditions.append("meeting_id = ?")
        params.append(meeting_id)
    if date_from:
        conditions.append("join_time >= ?")
        params.append(date_from)
    if date_to:
        conditions.append("join_time < ?")
        params.append(date_to)
    sql = f"""SELECT
        participant_name,
        email,
        COUNT(*) as session_count,
        SUM(duration_minutes) as total_duration_minutes,
        MIN(join_time) as first_join,
        MAX(leave_time) as last_leave
    FROM official_attendance_sessions
    WHERE {' AND '.join(conditions)}
    GROUP BY LOWER(participant_name), COALESCE(email, '')
    ORDER BY total_duration_minutes DESC"""
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def upsert_official_attendance_session(
    tenant_id: str,
    meeting_id: str,
    topic: str,
    host_name: str,
    host_email: str,
    meeting_start: str,
    meeting_end: str,
    participant_name: str,
    email: str,
    join_time: str,
    leave_time: str,
    duration_minutes: float,
) -> int:
    """写入一条官方报表 session，去重 key = tenant_id + meeting_id + participant_name + join_time + leave_time"""
    conn = _get_conn()
    cur = conn.execute(
        """INSERT OR IGNORE INTO official_attendance_sessions
        (tenant_id, meeting_id, topic, host_name, host_email,
         meeting_start_time, meeting_end_time,
         participant_name, email, join_time, leave_time,
         duration_minutes, source_file)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'report_api')""",
        (tenant_id, meeting_id, topic, host_name, host_email,
         meeting_start, meeting_end,
         participant_name, email, join_time, leave_time,
         duration_minutes),
    )
    conn.commit()
    return cur.lastrowid or 0


# ── History 上传页路由 ──
# (在 build_app() 中注入，不在此文件中定义)

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


def _make_user_key(name: str) -> str:
    return "".join(name.lower().split())


ROOM_SWITCH_ACTIONS_SET = frozenset({"enter", "joined", "breakout_enter", "breakout_leave"})
SESSION_SKIP_SET = frozenset({"breakout_enter", "breakout_leave", "waiting_room_enter", "admitted", "unknown"})


def check_room_switch(
    tenant_id: str, meeting_id: str, name: str, action_time_utc: str, window: int = 5
) -> bool:
    """
    检查 action_time 前后 window 秒内是否有房间切换事件。
    按 LOWER(REPLACE(name,' ','')) 归一化后匹配，
    避免 "Winifred" vs "winifred Winifred" 等不同写法导致漏判。
    """
    conn = _get_conn()
    try:
        dt = datetime.fromisoformat(action_time_utc.replace("Z", "+00:00"))
        start = (dt - timedelta(seconds=window)).isoformat()
        end = (dt + timedelta(seconds=window)).isoformat()
    except Exception:
        return False

    user_key = _make_user_key(name)
    row = conn.execute(
        f"""SELECT 1 FROM zoom_participants
            WHERE tenant_id = ?
              AND meeting_id = ?
              AND LOWER(REPLACE(name,' ','')) = ?
              AND action IN ({"," .join("?" for _ in ROOM_SWITCH_ACTIONS_SET)})
              AND action_time >= ?
              AND action_time <= ?
            LIMIT 1""",
        (tenant_id, meeting_id, user_key, *list(ROOM_SWITCH_ACTIONS_SET), start, end),
    ).fetchone()
    return row is not None


def save_participant_session(
    meeting_id: str,
    name: str,
    action: str,
    action_time: datetime,
    tenant_id: str = "default",
    source: str = "webhook",
) -> None:
    """
    根据参与者事件维护 participant_sessions。

    核心规则：
    - enter/joined → 无 open session 则 INSERT
    - leave/left   → 检查房间切换，非切换才关闭 session
    - breakout_* / waiting_room_* / admitted → 不修改 session
    """
    conn = _get_conn()
    action_str = action_time.isoformat()
    user_key = _make_user_key(name)

    if action in SESSION_SKIP_SET:
        return

    if action in ("enter", "joined"):
        existing = conn.execute(
            """SELECT id FROM participant_sessions
               WHERE tenant_id = ? AND meeting_id = ? AND user_key = ? AND leave_time_utc IS NULL
               LIMIT 1""",
            (tenant_id, meeting_id, user_key),
        ).fetchone()
        if existing:
            return
        conn.execute(
            """INSERT INTO participant_sessions
               (meeting_id, user_key, user_name, tenant_id, join_time_utc, leave_time_utc, duration_seconds, source, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, NULL, 0, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)""",
            (meeting_id, user_key, name, tenant_id, action_str, source),
        )
        conn.commit()
        return

    if action in ("leave", "left"):
        if check_room_switch(tenant_id, meeting_id, name, action_str):
            return

        existing = conn.execute(
            """SELECT id, join_time_utc FROM participant_sessions
               WHERE tenant_id = ? AND user_key = ? AND leave_time_utc IS NULL
               ORDER BY join_time_utc DESC LIMIT 1""",
            (tenant_id, user_key),
        ).fetchone()
        if not existing:
            return

        session_id, join_utc = existing
        try:
            join_dt = datetime.fromisoformat(join_utc.replace("Z", "+00:00"))
            leave_dt = action_time if action_time.tzinfo else action_time.replace(tzinfo=timezone.utc)
            duration = max(0, int((leave_dt - join_dt).total_seconds()))
        except Exception:
            duration = 0

        conn.execute(
            """UPDATE participant_sessions
               SET leave_time_utc = ?, duration_seconds = ?, updated_at = CURRENT_TIMESTAMP
               WHERE id = ?""",
            (action_str, duration, session_id),
        )
        conn.commit()
        return


def get_today_from_sessions(
    tenant_id: str,
    member_key: str | None = None,
) -> list[dict]:
    """
    基于 participant_sessions + current_member_sessions 的今日累计查询（MYT 业务日，切割点 06:00）。

    today_total_seconds = closed_sessions_overlap + current_live_open_duration
    - closed_sessions_overlap: participant_sessions 中 closed session 与今日窗口的重叠秒数
    - current_live_open_duration: 仅来自 current_member_sessions.is_online=1 且 open_session_started_at 非空
    """
    conn = _get_conn()
    br = get_business_day_range_myt(6)
    today_start = br["start_utc"]
    today_end = br["end_utc"]
    today_start_iso = today_start.isoformat()
    today_end_iso = today_end.isoformat()
    now_utc = datetime.now(timezone.utc)
    cutoff = min(now_utc, today_end)

    def _parse_dt(value):
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    def _overlap_seconds(start_value, end_value) -> int:
        start_dt = _parse_dt(start_value)
        end_dt = _parse_dt(end_value)
        if not start_dt or not end_dt:
            return 0
        a = max(start_dt, today_start)
        b = min(end_dt, today_end)
        if b <= a:
            return 0
        return int((b - a).total_seconds())

    members: dict[str, dict] = {}

    session_params = [tenant_id, today_end_iso, today_start_iso]
    member_filter = ""
    if member_key:
        member_filter = " AND user_key = ?"
        session_params.append(member_key)

    session_rows = conn.execute(
        "SELECT user_key, user_name, join_time_utc, leave_time_utc, duration_seconds "
        "FROM participant_sessions "
        "WHERE tenant_id = ? "
        "AND join_time_utc < ? "
        "AND COALESCE(leave_time_utc, join_time_utc) >= ? "
        "AND leave_time_utc IS NOT NULL "
        f"{member_filter} "
        "ORDER BY user_key, join_time_utc",
        session_params,
    ).fetchall()

    for row in session_rows:
        user_key = row["user_key"]
        if not user_key:
            continue
        m = members.setdefault(user_key, {
            "user_name": row["user_name"] or user_key,
            "user_key": user_key,
            "closed_seconds": 0,
            "live_seconds": 0,
            "is_online": False,
            "first_join_today": "",
            "last_activity": "",
            "open_session_started_at": "",
        })
        overlap = _overlap_seconds(row["join_time_utc"], row["leave_time_utc"])
        m["closed_seconds"] += overlap
        join_dt = _parse_dt(row["join_time_utc"])
        leave_dt = _parse_dt(row["leave_time_utc"])
        if join_dt and today_start <= join_dt < today_end:
            if not m["first_join_today"] or str(row["join_time_utc"]) < m["first_join_today"]:
                m["first_join_today"] = row["join_time_utc"]
        if leave_dt and (not m["last_activity"] or str(row["leave_time_utc"]) > m["last_activity"]):
            m["last_activity"] = row["leave_time_utc"]

    cms_params = [tenant_id]
    cms_filter = ""
    if member_key:
        cms_filter = " AND member_key = ?"
        cms_params.append(member_key)
    cms_rows = conn.execute(
        "SELECT member_key, display_name, is_online, open_session_started_at, first_join_at, last_activity_at "
        "FROM current_member_sessions "
        "WHERE tenant_id = ? AND is_online = 1 AND open_session_started_at IS NOT NULL AND open_session_started_at != '' "
        f"{cms_filter}",
        cms_params,
    ).fetchall()

    for row in cms_rows:
        user_key = row["member_key"]
        if not user_key:
            continue
        m = members.setdefault(user_key, {
            "user_name": row["display_name"] or user_key,
            "user_key": user_key,
            "closed_seconds": 0,
            "live_seconds": 0,
            "is_online": False,
            "first_join_today": "",
            "last_activity": "",
            "open_session_started_at": "",
        })
        m["user_name"] = row["display_name"] or m["user_name"]
        m["is_online"] = True
        m["open_session_started_at"] = row["open_session_started_at"] or ""
        live_start = _parse_dt(row["open_session_started_at"])
        if live_start:
            a = max(live_start, today_start)
            b = cutoff
            if b > a:
                m["live_seconds"] = int((b - a).total_seconds())
        first_join = row["first_join_at"] or row["open_session_started_at"] or ""
        if first_join and (not m["first_join_today"] or str(first_join) < m["first_join_today"]):
            m["first_join_today"] = first_join
        last_activity = row["last_activity_at"] or row["open_session_started_at"] or ""
        if last_activity and (not m["last_activity"] or str(last_activity) > m["last_activity"]):
            m["last_activity"] = last_activity

    results = []
    for m in members.values():
        total = int(m["closed_seconds"] + m["live_seconds"])
        results.append({
            "user_name": m["user_name"],
            "user_key": m["user_key"],
            "total_seconds": total,
            "closed_seconds": int(m["closed_seconds"]),
            "is_online": bool(m["is_online"]),
            "first_join_today": m["first_join_today"] or "",
            "last_activity": m["last_activity"] or "",
            "open_session_started_at": m["open_session_started_at"] or "",
        })

    return results


def compare_session_vs_events(
    tenant_id: str, names: list[str] | None = None
) -> list[dict]:
    """DEBUG: 对比 session 查询 vs 事件推导的结果差异。"""
    sessions = get_today_from_sessions(tenant_id)
    comparison = []

    for s in sessions:
        if names and s["user_key"] not in [_make_user_key(n) for n in names]:
            continue
        comparison.append({
            "user_name": s["user_name"],
            "user_key": s["user_key"],
            "session_seconds": s["total_seconds"],
            "session_closed": s["closed_seconds"],
            "session_online": s["is_online"],
            "session_open_join": s["open_session_started_at"],
        })

    return comparison


def _build_session_summary(
    session_rows: list[dict], tenant_id: str | None = None
) -> dict:
    """将 get_today_from_sessions() 的结果转换成 get_today_attendance_summary() 兼容格式。"""
    from datetime import datetime, timezone, timedelta
    from collections import OrderedDict

    conn = _get_conn()
    br_sess = get_business_day_range_myt(6)
    business_date = br_sess["business_date"]

    # ── 批量加载成员→分组映射 ──
    _group_map_cache = {}
    if tenant_id:
        grp_rows = conn.execute(
            "SELECT DISTINCT md.raw_name, md.display_name, COALESCE(g.name, '') AS group_name, "
            "g.id AS group_id "
            "FROM member_display md "
            "LEFT JOIN member_groups g ON g.id = md.group_id AND g.tenant_id = md.tenant_id "
            "WHERE md.tenant_id = ? AND md.group_id IS NOT NULL AND md.deleted=0",
            (tenant_id,),
        ).fetchall()
    else:
        grp_rows = conn.execute(
            "SELECT DISTINCT md.raw_name, md.display_name, COALESCE(g.name, '') AS group_name, "
            "g.id AS group_id, md.tenant_id "
            "FROM member_display md "
            "LEFT JOIN member_groups g ON g.id = md.group_id AND g.tenant_id = md.tenant_id "
            "WHERE md.group_id IS NOT NULL AND md.deleted=0"
        ).fetchall()
    for gr in grp_rows:
        grd = dict(gr)
        grp_name = grd.get("group_name", "")
        grp_id = grd.get("group_id")
        t_id = grd.get("tenant_id") or tenant_id
        for key in (
            grd.get("raw_name", "").strip().lower().replace(" ", ""),
            grd.get("display_name", "").strip().lower().replace(" ", ""),
        ):
            if key:
                _group_map_cache[(t_id, key)] = (grp_name, grp_id)

    # ── 批量加载显示名映射 ──
    name_map_cache = {}
    if tenant_id:
        md_rows = conn.execute(
            "SELECT raw_name, display_name, aliases FROM member_display WHERE tenant_id=? AND deleted=0",
            (tenant_id,),
        ).fetchall()
    else:
        md_rows = conn.execute(
            "SELECT raw_name, display_name, aliases, tenant_id FROM member_display WHERE deleted=0"
        ).fetchall()
    for mr in md_rows:
        mrd = dict(mr)
        t_id = mrd.get("tenant_id") or tenant_id
        disp = mrd.get("display_name") or mrd.get("raw_name", "")
        # 跳过 (2) 变体记录 - 如果该 key 已被主记录占用则跳过
        is_variant = disp.endswith(" (2)")
        for alias in [mrd.get("raw_name", ""), disp]:
            key = alias.strip().lower().replace(" ", "")
            if not key:
                continue
            existing = name_map_cache.get((t_id, key))
            if existing and is_variant:
                continue  # 主记录优先，跳过变体
            if is_variant and not existing:
                continue  # 没有主记录也不写变体
            name_map_cache[(t_id, key)] = disp
        for alias in json.loads(mrd.get("aliases") or "[]"):
            key = alias.strip().lower().replace(" ", "")
            if not key:
                continue
            existing = name_map_cache.get((t_id, key))
            if existing and is_variant:
                continue
            if is_variant and not existing:
                continue
            name_map_cache[(t_id, key)] = disp

    # ── 批量加载 join_count / leave_count（从 zoom_participants） ──
    # 按 name_map_cache 合并标准化的 user_key 和 raw_name 的映射
    _join_leave_count = {}  # user_key → {"join_count": N, "leave_count": N}
    br_sess_lu = get_business_day_range_myt(6)
    today_start_iso_zl = br_sess_lu["start_utc"].isoformat()
    today_end_iso_zl = br_sess_lu["end_utc"].isoformat()

    if tenant_id:
        zp_raw = conn.execute(
            "SELECT name, action, COUNT(*) AS cnt FROM zoom_participants "
            "WHERE tenant_id=? AND action_time>=? AND action_time<? "
            "AND action IN ('enter','joined','leave','left') "
            "GROUP BY name, action",
            (tenant_id, today_start_iso_zl, today_end_iso_zl),
        ).fetchall()
    else:
        zp_raw = conn.execute(
            "SELECT name, action, COUNT(*) AS cnt FROM zoom_participants "
            "WHERE action_time>=? AND action_time<? "
            "AND action IN ('enter','joined','leave','left') "
            "GROUP BY name, action",
            (today_start_iso_zl, today_end_iso_zl),
        ).fetchall()

    for zr in zp_raw:
        zrd = dict(zr)
        raw_name_key = zrd["name"].strip().lower().replace(" ", "")
        # 通过 name_map_cache 拿到 display_name 作为 user_key
        user_key_for_count = name_map_cache.get((tenant_id or "", raw_name_key), zrd["name"])
        user_key_for_count = user_key_for_count.strip().lower().replace(" ", "")
        if user_key_for_count not in _join_leave_count:
            _join_leave_count[user_key_for_count] = {"join_count": 0, "leave_count": 0}
        act = zrd["action"]
        cnt = zrd["cnt"]
        if act in ("enter", "joined"):
            _join_leave_count[user_key_for_count]["join_count"] += cnt
        elif act in ("leave", "left"):
            _join_leave_count[user_key_for_count]["leave_count"] += cnt

    members = OrderedDict()
    deleted_names = _load_deleted_names(tenant_id)

    for s in session_rows:
        user_key = s["user_key"]
        if not user_key:
            continue
        # 跳过被软删除的
        if user_key in deleted_names:
            continue
        user_name = s["user_name"]
        total_seconds = s["total_seconds"]
        is_online = s["is_online"]
        first_join = s["first_join_today"]
        last_activity = s["last_activity"]
        open_join = s.get("open_session_started_at", "")
        standard_name = name_map_cache.get((tenant_id, user_key), user_name or user_key)
        grp_name, grp_id = _group_map_cache.get((tenant_id, user_key), ("", None))
        if not grp_name and standard_name:
            user_key2 = standard_name.strip().lower().replace(" ", "")
            grp_name, grp_id = _group_map_cache.get((tenant_id, user_key2), ("", None))
        last_leave = ""
        if not is_online and last_activity:
            last_leave = last_activity
        # 从 zoom_participants 拿 join/leave 计数
        zl = _join_leave_count.get(standard_name.strip().lower().replace(" ", ""), {"join_count": 0, "leave_count": 0})
        members[user_key] = {
            "name": user_name or user_key,
            "raw_name": user_name or user_key,
            "standard_name": standard_name,
            "today_total_seconds": total_seconds,
            "today_total_duration": _fmt_dur(int(total_seconds)),
            "is_online": is_online,
            "status": "online" if is_online else "offline",
            "session_count": zl["join_count"],
            "disconnect_count": zl["leave_count"],
            "join_count": zl["join_count"],
            "leave_count": zl["leave_count"],
            "first_join": first_join,
            "first_join_display": _myt_short(first_join) if first_join else "",
            "last_activity": last_activity,
            "last_activity_display": _myt_short(last_activity) if last_activity else "",
            "last_leave": last_leave,
            "last_leave_display": _myt_short(last_leave) if last_leave else "",
            "last_leave_time_display": _myt_short(last_leave) if last_leave else "",
            "email": "",
            "group_name": grp_name,
            "group_id": grp_id,
            "tenant_id": tenant_id or "",
            "open_session_started_at": open_join,
            "raw_events": [],
        }

    # ── 第二步：从 zoom_participants 补充有今日事件但无 session 的人 ──
    br_zp = get_business_day_range_myt(6)  # 复用 br 用于跨日查询
    today_start_zp = br_zp["start_utc"].isoformat()
    today_end_zp = br_zp["end_utc"].isoformat()
    business_day_start = br_zp["start_utc"]  # datetime 类型，用于隐含 enter
    now_utc_zp = datetime.now(timezone.utc)

    # 提前获取实时在线列表，用于 zoom_participants open session 判断
    try:
        online_names = get_live_online_standard_names(tenant_id) if tenant_id else set()
    except Exception:
        online_names = set()

    if tenant_id:
        zp_rows = conn.execute(
            "SELECT DISTINCT name FROM zoom_participants "
            "WHERE tenant_id=? AND action_time>=? AND action_time<? "
            "AND action IN ('enter','joined','leave','left')",
            (tenant_id, today_start_zp, today_end_zp),
        ).fetchall()
    else:
        zp_rows = conn.execute(
            "SELECT DISTINCT name FROM zoom_participants "
            "WHERE action_time>=? AND action_time<? "
            "AND action IN ('enter','joined','leave','left')",
            (today_start_zp, today_end_zp),
        ).fetchall()

    existing_keys = set(members.keys())
    for zpr in zp_rows:
        raw_name = zpr["name"]
        resolved = resolve_display_name(raw_name, tenant_id)
        standard_name = resolved["display_name"]
        if not standard_name:
            continue
        user_key = standard_name.strip().lower().replace(" ", "")
        if user_key in deleted_names or user_key in existing_keys:
            continue

        # ── 获取业务日内所有 enter/leave 事件（含全部 action 类型用于上下文） ──
        if tenant_id:
            all_events = conn.execute(
                "SELECT action, action_time FROM zoom_participants "
                "WHERE tenant_id=? AND name=? AND action_time>=? AND action_time<? "
                "ORDER BY action_time",
                (tenant_id, raw_name, today_start_zp, today_end_zp),
            ).fetchall()
        else:
            all_events = conn.execute(
                "SELECT action, action_time FROM zoom_participants "
                "WHERE name=? AND action_time>=? AND action_time<? "
                "ORDER BY action_time",
                (raw_name, today_start_zp, today_end_zp),
            ).fetchall()
        # 过滤出 main enter/leave 用于遍历
        raw_events = [e for e in all_events if e["action"] in ("enter", "joined", "leave", "left")]

        # ── 查跨业务日 open session：业务日 start 前最后一条事件是否为 enter ──
        prev_row = conn.execute(
            "SELECT action, action_time FROM zoom_participants "
            "WHERE name=? AND action_time<? "
            "AND action IN ('enter','joined','leave','left') "
            "ORDER BY action_time DESC LIMIT 1",
            (raw_name, today_start_zp),
        ).fetchone()
        last_was_enter = prev_row is not None and prev_row["action"] in ("enter", "joined")

        # ── 配对计算时长，只量 main session enter/leave ──
        # 规则：
        #   - 只看 enter/leave 事件（排除 breakout_enter/breakout_leave/waiting_room/admitted）
        #   - 同秒 leave+enter 视为主 session 持续（不是真正的断线）
        #   - 跨日：如果业务日 start 前最后是 enter，从 start_utc 开始 pending
        zp_total = 0
        zp_join = 0
        zp_leave = 0
        zp_first_join = None
        zp_last_activity = None
        pending = None
        if last_was_enter:
            # 跨业务日：隐含 pending 从业务日开始
            pending = business_day_start
            zp_last_activity = today_start_zp

        # ── 预计算：哪些 leave 是 break room 关闭的副作用 ──
        # 在 all_events 中找 "breakout_xxx 同秒 leave，且有后续 main enter" 的组合
        skip_leave_set = set()  # 存放 raw_events 中应跳过的 idx
        for ae_i in range(1, len(all_events)):
            prev_ae = all_events[ae_i - 1]
            curr_ae = all_events[ae_i]
            # 匹配 breakout_leave + leave 或 breakout_enter + leave 同秒
            if curr_ae["action"] in ("leave", "left") and \
               prev_ae["action"] in ("breakout_leave", "breakout_enter") and \
               str(curr_ae["action_time"]) == str(prev_ae["action_time"]):
                # 检查同一天是否有后续 main enter
                for later_ae in all_events[ae_i+1:]:
                    if later_ae["action"] in ("enter", "joined"):
                        # 有后续 main enter → break room 关闭的副作用，非真正离开
                        for re_i, re in enumerate(raw_events):
                            if re["action"] == curr_ae["action"] and str(re["action_time"]) == str(curr_ae["action_time"]):
                                skip_leave_set.add(re_i)
                        break  # 只取第一个匹配的 leave

        # ── 同秒 leave+enter 检测 ──
        i = 0
        while i < len(raw_events):
            re = raw_events[i]
            action = re["action"]
            ts = re["action_time"]
            try:
                at_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except:
                at_dt = datetime.fromisoformat(str(ts))

            # 检查同秒 leave→enter 模式
            if action in ("leave", "left") and i + 1 < len(raw_events):
                next_re = raw_events[i + 1]
                next_ts = str(next_re["action_time"])
                if next_re["action"] in ("enter", "joined") and next_ts == str(ts):
                    # 同秒 leave+enter → 这不是真的断线，是 breakout 恢复
                    # 不增加 leave_count，不关闭 pending
                    # 跳到 next_re 当作 enter
                    zp_last_activity = next_ts
                    if pending is None:
                        # 如果 pending 为空，说明这个同秒 enter 是起始
                        try:
                            pending = datetime.fromisoformat(str(next_ts).replace("Z", "+00:00"))
                        except:
                            pending = datetime.fromisoformat(str(next_ts))
                    if zp_first_join is None:
                        zp_first_join = next_ts
                    i += 2
                    continue

            if action in ("enter", "joined"):
                zp_join += 1
                if zp_first_join is None:
                    zp_first_join = ts
                if pending is None:
                    pending = at_dt
                zp_last_activity = ts
            elif action in ("leave", "left"):
                zp_last_activity = ts
                if i in skip_leave_set:
                    # break room 关闭副作用，且同天有后续 main enter
                    # 不递增 leave_count，但关闭 pending（停止跨日累计）
                    # pending 会在后续 enter 重新打开
                    if pending is not None:
                        dur = (at_dt - pending).total_seconds()
                        if 0 < dur < 86400:
                            zp_total += dur
                        pending = None
                elif pending is not None:
                    dur = (at_dt - pending).total_seconds()
                    if 0 < dur < 86400:
                        zp_total += dur
                    pending = None
            i += 1

        # ── 处理业务日结束仍未关闭的 open session ──
        if pending is not None:
            is_online_zp = False
            cutoff = now_utc_zp
            if zp_last_activity:
                try:
                    last_dt = datetime.fromisoformat(str(zp_last_activity).replace("Z", "+00:00"))
                    if (now_utc_zp - last_dt).total_seconds() < 900:
                        is_online_zp = True
                        cutoff = now_utc_zp
                    elif standard_name in online_names:
                        # 实时在线，即使 last_activity 较旧也按 now 截断
                        is_online_zp = True
                        cutoff = now_utc_zp
                    else:
                        cutoff = last_dt
                        is_online_zp = False
                except:
                    is_online_zp = False
            open_dur = (cutoff - pending).total_seconds()
            if 0 < open_dur < 86400:
                zp_total += open_dur
        else:
            is_online_zp = False
            if zp_last_activity:
                try:
                    last_dt = datetime.fromisoformat(str(zp_last_activity).replace("Z", "+00:00"))
                    if (now_utc_zp - last_dt).total_seconds() < 900:
                        is_online_zp = True
                except:
                    pass

        grp_name_zp, grp_id_zp = _group_map_cache.get((tenant_id, user_key), ("", None))
        if not grp_name_zp and standard_name:
            grp_name_zp, grp_id_zp = _group_map_cache.get((tenant_id, standard_name.strip().lower().replace(" ", "")), ("", None))

        members[user_key] = {
            "name": raw_name,
            "raw_name": raw_name,
            "standard_name": standard_name,
            "today_total_seconds": int(zp_total),
            "today_total_duration": _fmt_dur(int(zp_total)),
            "is_online": is_online_zp,
            "status": "online" if is_online_zp else "offline",
            "session_count": zp_join,
            "disconnect_count": zp_leave,
            "join_count": zp_join,
            "leave_count": zp_leave,
            "first_join": zp_first_join or "",
            "first_join_display": _myt_short(zp_first_join) if zp_first_join else "",
            "last_activity": zp_last_activity or "",
            "last_activity_display": _myt_short(zp_last_activity) if zp_last_activity else "",
            "last_leave": zp_last_activity if not is_online_zp else "",
            "last_leave_display": _myt_short(zp_last_activity) if zp_last_activity and not is_online_zp else "",
            "last_leave_time_display": _myt_short(zp_last_activity) if zp_last_activity and not is_online_zp else "",
            "email": "",
            "group_name": grp_name_zp,
            "group_id": grp_id_zp,
            "tenant_id": tenant_id or "",
            "open_session_started_at": "",
            "raw_events": [],
        }
        existing_keys.add(user_key)

    # ── 第三步：补充 live online 中仍然无记录的成员 ──
    for on_name in online_names:
        on_key = on_name.strip().lower().replace(" ", "")
        if on_key in deleted_names or on_key in existing_keys:
            continue
        grp_name_on, grp_id_on = _group_map_cache.get((tenant_id, on_key), ("", None))
        if not grp_name_on:
            grp_name_on, grp_id_on = _group_map_cache.get((tenant_id, on_key), ("", None))
        members[on_key] = {
            "name": on_name,
            "raw_name": on_name,
            "standard_name": on_name,
            "today_total_seconds": 0,
            "today_total_duration": "0m",
            "is_online": True,
            "status": "online",
            "session_count": 0,
            "disconnect_count": 0,
            "join_count": 0,
            "leave_count": 0,
            "first_join": "",
            "first_join_display": "",
            "last_activity": "",
            "last_activity_display": "",
            "last_leave": "",
            "last_leave_display": "",
            "last_leave_time_display": "",
            "email": "",
            "group_name": grp_name_on,
            "group_id": grp_id_on,
            "tenant_id": tenant_id or "",
            "open_session_started_at": "",
            "raw_events": [],
        }
        existing_keys.add(on_key)

    # ── 排序：在线优先 → 时长降序 ──
    sorted_members = sorted(
        members.values(),
        key=lambda m: (0 if m["status"] == "online" else 1, -(m["today_total_seconds"] or 0)),
    )

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
        "date": business_date,
        "members": sorted_members,
    }


def _load_deleted_names(tenant_id: str | None = None) -> set:
    """加载被软删除的成员 user_key 集合。

    注意：如果一个 deleted 记录的 raw_name / alias 已经被 active（deleted=0）记录
    的 match_key、display_name 或 aliases 占用，则不加入删除名单。
    """
    conn = _get_conn()

    # 先加载所有 active 记录的 match_key + display_name + aliases
    if tenant_id:
        active_rows = conn.execute(
            "SELECT match_key, display_name, aliases FROM member_display WHERE tenant_id=? AND deleted=0",
            (tenant_id,),
        ).fetchall()
        rows = conn.execute(
            "SELECT raw_name, display_name, aliases FROM member_display WHERE tenant_id=? AND deleted=1",
            (tenant_id,),
        ).fetchall()
    else:
        active_rows = conn.execute(
            "SELECT match_key, display_name, aliases FROM member_display WHERE deleted=0"
        ).fetchall()
        rows = conn.execute(
            "SELECT raw_name, display_name, aliases FROM member_display WHERE deleted=1"
        ).fetchall()

    # 构建 active 占用的所有 key 集合
    active_keys = set()
    for ar in active_rows:
        mk = ar["match_key"]
        if mk:
            active_keys.add(mk.strip().lower().replace(" ", ""))
        dn = ar["display_name"]
        if dn:
            active_keys.add(dn.strip().lower().replace(" ", ""))
        for alias in json.loads(ar["aliases"] or "[]"):
            if alias:
                active_keys.add(alias.strip().lower().replace(" ", ""))

    # 构建 deleted 集合，排除已被 active 占用的 key
    deleted = set()
    for dr in rows:
        for n in (dr["raw_name"], dr["display_name"]):
            if n:
                key = n.strip().lower().replace(" ", "")
                if key not in active_keys:
                    deleted.add(key)
        for alias in json.loads(dr["aliases"] or "[]"):
            if alias:
                key = alias.strip().lower().replace(" ", "")
                if key not in active_keys:
                    deleted.add(key)
    return deleted


def debug_participant_session(
    tenant_id: str, name: str
) -> dict:
    """
    DEBUG: 查看某个用户今天的所有 session 记录和 zoom_participants 事件流。
    """
    conn = _get_conn()
    myt_now = datetime.now(timezone.utc) + timedelta(hours=8)
    today_start = myt_now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(hours=8)
    today_start_iso = today_start.isoformat()
    user_key = _make_user_key(name)

    sessions = conn.execute(
        """SELECT * FROM participant_sessions
           WHERE tenant_id = ? AND user_key = ? AND join_time_utc >= ?
           ORDER BY join_time_utc""",
        (tenant_id, user_key, today_start_iso),
    ).fetchall()

    all_names = [name]
    for sn in conn.execute(
        "SELECT DISTINCT name FROM zoom_participants WHERE tenant_id=? AND LOWER(REPLACE(name,' ',''))=? AND action_time>=?",
        (tenant_id, user_key, today_start_iso),
    ).fetchall():
        sn_val = sn[0]
        if sn_val not in all_names:
            all_names.append(sn_val)

    placeholders = ",".join("?" for _ in all_names)
    events = conn.execute(
        f"""SELECT id, meeting_id, name, action, action_time, source
            FROM zoom_participants
            WHERE tenant_id = ? AND action_time >= ? AND name IN ({placeholders})
            ORDER BY action_time""",
        (tenant_id, today_start_iso, *all_names),
    ).fetchall()

    return {
        "user_key": user_key,
        "names_searched": all_names,
        "sessions": [dict(s) for s in sessions],
        "events_count": len(events),
        "events": [
            {"id": e["id"], "meeting_id": e["meeting_id"],
             "action": e["action"], "action_time": e["action_time"],
             "source": e["source"]}
            for e in events
        ],
    }


def get_today_participants(limit: int = 200, tenant_id: str = None) -> list[dict]:
    # MYT 日历日开始: 今天的 MYT 00:00 转为 UTC
    myt_now = datetime.now(timezone.utc) + timedelta(hours=8)
    myt_midnight = myt_now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_utc_start = myt_midnight - timedelta(hours=8)
    conn = _get_conn()
    if tenant_id:
        rows = conn.execute(
            "SELECT * FROM zoom_participants WHERE action_time >= ? AND tenant_id = ? ORDER BY action_time DESC LIMIT ?",
            (today_utc_start.isoformat(), tenant_id, limit),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM zoom_participants WHERE action_time >= ? ORDER BY action_time DESC LIMIT ?",
            (today_utc_start.isoformat(), limit),
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


def get_business_day_range_myt(cutoff_hour: int = 6) -> dict:
    """基于 MYT 业务日（自定义分割点）返回查询窗口。

    规则：
      - 当前 MYT 时间 < cutoff_hour（凌晨 00:00-05:59）：查询窗口是 **上一** 业务日
      - 当前 MYT 时间 >= cutoff_hour（06:00-23:59）：查询窗口是 **当天** 业务日

    业务日定义：MYT cutoff_hour:00 ~ 次日 MYT cutoff_hour:00
    转换为 UTC 即：前一日的 (24-cutoff_hour):00 UTC ~ 当日 (24-cutoff_hour):00 UTC

    例：cutoff_hour=6，当前 MYT=2026-06-25 02:30（凌晨）
      → 上一业务日：MYT 2026-06-24 06:00 ~ 2026-06-25 06:00
      → UTC: 2026-06-23 22:00 ~ 2026-06-24 22:00
      → business_date = "2026-06-24"（MYT 日历日）

    返回:
      {
        "start_utc": datetime (timezone-aware UTC),
        "end_utc": datetime (timezone-aware UTC),
        "business_date": str (MYT 日历日, e.g. "2026-06-24"),
      }
    """
    from datetime import datetime, timezone, timedelta

    # MYT now 在 06:00-23:59 → 当天业务日；00:00-05:59 → 上一业务日
    now_utc = datetime.now(timezone.utc)
    now_myt = now_utc + timedelta(hours=8)

    if now_myt.hour >= cutoff_hour:
        # 当天 MYT 06:00 - 23:59 → 业务日 = 当天 MYT 日历日
        business_day_start_myt = now_myt.replace(
            hour=cutoff_hour, minute=0, second=0, microsecond=0
        )
        business_date = now_myt.strftime("%Y-%m-%d")
    else:
        # 凌晨 00:00 - 05:59 → 业务日 = 上一 MYT 日历日
        yesterday_myt = now_myt - timedelta(days=1)
        business_day_start_myt = yesterday_myt.replace(
            hour=cutoff_hour, minute=0, second=0, microsecond=0
        )
        business_date = yesterday_myt.strftime("%Y-%m-%d")

    start_utc = business_day_start_myt - timedelta(hours=8)  # 转回 UTC
    end_utc = start_utc + timedelta(hours=24)

    return {
        "start_utc": start_utc,
        "end_utc": end_utc,
        "business_date": business_date,
    }


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




def get_today_attendance_summary(tenant_id: str = None, meeting_id: str = None, session_start_after: str = None) -> dict:
    """今日参会汇总 — 统一基于 participant_sessions + sharing_live + CMS fallback

    统计规则：
    1. participant_sessions 是主来源，每条 session 贡献 overlap 秒数
       - leave_time 有值 → closed session
       - leave_time 为空 → open session (持续到 now)
    2. sharing_live is_active=1 只作为 fallback：
       - 如果成员没有 open participant_session，用 sharing_live.start_time 作为 open session
       - 如果成员已有 open participant_session，不重复计算 sharing_live
    3. current_member_sessions 作为三级 fallback
       - 成员没有任何 participant_session 时使用
    """
    from datetime import datetime, timezone, timedelta
    from collections import OrderedDict

    now_utc = datetime.now(timezone.utc)
    br = get_business_day_range_myt(6)
    today_start_utc = br["start_utc"]
    today_end_utc = br["end_utc"]
    today_start_myt = br["start_utc"] + timedelta(hours=8)

    def _parse_dt(value):
        if not value:
            return None
        try:
            dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt

    def _overlap_seconds(start_dt, end_dt) -> int:
        if not start_dt or not end_dt:
            return 0
        a = max(start_dt, today_start_utc)
        b = min(end_dt, today_end_utc)
        if b <= a:
            return 0
        return int((b - a).total_seconds())

    def _init_member(display_name):
        return {
            "standard_name": display_name,
            "group_name": get_member_group(display_name) or "",
            "status": "offline",
            "first_join": None,
            "today_total_seconds": 0,
            "today_total_duration": "0m",
            "session_count": 0,
            "disconnect_count": 0,
            "last_activity": None,
            "last_leave_time": None,
            "open_session_started_at": None,
            "open_from_ps": False,
            "open_from_sl": False,
            "open_from_cms": False,
            "last_activity_display": "",
            "first_join_display": "",
            "email": "",
        }

    conn = _get_conn()
    members = OrderedDict()

    # ── Step 1: participant_sessions (ALL, including open) ──
    where_extra = ""
    params = [tenant_id, today_end_utc.isoformat(), today_start_utc.isoformat()]
    if meeting_id:
        where_extra = " AND meeting_id = ?"
        params.append(meeting_id)

    # Get ALL sessions that overlap today (closed or open)
    session_rows = conn.execute(
        "SELECT user_key, user_name, join_time_utc, leave_time_utc, duration_seconds, meeting_id "
        "FROM participant_sessions "
        "WHERE tenant_id = ? "
        "AND join_time_utc < ? "
        "AND COALESCE(leave_time_utc, join_time_utc) >= ? "
        + where_extra +
        "ORDER BY user_key, join_time_utc",
        params,
    ).fetchall()

    # Track which members have an open participant_session
    members_with_open_ps = set()

    for row in session_rows:
        user_key = row["user_key"]
        if not user_key:
            continue
        if session_start_after and str(row["join_time_utc"]) < session_start_after:
            continue

        display_name = resolve_display_name(row["user_name"] or user_key)["display_name"]
        if display_name not in members:
            members[display_name] = _init_member(display_name)
        m = members[display_name]

        join_dt = _parse_dt(row["join_time_utc"])
        leave_dt = _parse_dt(row["leave_time_utc"])

        if leave_dt:
            # Closed session
            end_dt = leave_dt
            overlap = _overlap_seconds(join_dt, end_dt)
            m["today_total_seconds"] += overlap
            m["session_count"] += 1
            if m["last_leave_time"] is None or leave_dt > m["last_leave_time"]:
                m["last_leave_time"] = leave_dt
            if m["last_activity"] is None or leave_dt > m["last_activity"]:
                m["last_activity"] = leave_dt
        else:
            # Open session (still ongoing)
            end_dt = now_utc
            overlap = _overlap_seconds(join_dt, end_dt)
            m["today_total_seconds"] += overlap
            m["session_count"] += 1
            m["status"] = "online"
            m["open_session_started_at"] = join_dt
            m["open_from_ps"] = True
            m["last_activity"] = now_utc
            m["last_leave_time"] = None
            members_with_open_ps.add(display_name)

        # first_join: earliest among all sessions
        if join_dt and (m["first_join"] is None or join_dt < m["first_join"]):
            m["first_join"] = join_dt

    # ── Step 2: sharing_live is_active=1 (fallback for members WITHOUT open participant_session) ──
    if not meeting_id:
        sl_rows = conn.execute(
            "SELECT user_name, meeting_id, start_time "
            "FROM sharing_live "
            "WHERE tenant_id = ? AND is_active = 1 AND start_time IS NOT NULL",
            (tenant_id,),
        ).fetchall()

        for ar in sl_rows:
            user_name = ar[0]
            start_time = ar[2]
            st = _parse_dt(start_time)
            if not st:
                continue

            display_name = resolve_display_name(user_name)["display_name"]

            if display_name in members_with_open_ps:
                # Member already has open participant_session — do NOT add sharing_live duration
                # But do update status/activity if needed
                m = members[display_name]
                m["status"] = "online"
                m["last_activity"] = now_utc
                continue

            if display_name not in members:
                members[display_name] = _init_member(display_name)
            m = members[display_name]

            overlap = _overlap_seconds(st, now_utc)
            m["today_total_seconds"] += overlap
            m["session_count"] += 1
            m["status"] = "online"
            m["open_session_started_at"] = st
            m["open_from_sl"] = True
            m["last_activity"] = now_utc
            m["last_leave_time"] = None

            if m["first_join"] is None or st < m["first_join"]:
                m["first_join"] = st

    # ── Step 3: current_member_sessions (tertiary fallback) ──
    if not meeting_id:
        cms_rows = conn.execute(
            "SELECT member_key, display_name, is_online, open_session_started_at, "
            "first_join_at, last_activity_at, join_count, leave_count "
            "FROM current_member_sessions "
            "WHERE tenant_id = ? AND is_online = 1 AND open_session_started_at IS NOT NULL "
            "AND open_session_started_at != ''",
            (tenant_id,),
        ).fetchall()

        for row in cms_rows:
            display_name = row["display_name"]
            if not display_name:
                continue

            if display_name in members:
                m = members[display_name]
                # CMS supplements data only for members who have NO open ps or sharing
                if m.get("open_from_ps") or m.get("open_from_sl"):
                    # Already has open session from better source, just update status
                    m["status"] = "online"
                    continue

            # Either no member record, or member has only closed sessions
            if display_name not in members:
                members[display_name] = _init_member(display_name)
            m = members[display_name]

            cms_open = _parse_dt(row["open_session_started_at"])
            if cms_open:
                overlap = _overlap_seconds(cms_open, now_utc)
                m["today_total_seconds"] += overlap
                m["session_count"] += row["join_count"] or 0
                m["disconnect_count"] += row["leave_count"] or 0
                m["status"] = "online"
                m["open_session_started_at"] = cms_open
                m["open_from_cms"] = True
                m["last_activity"] = now_utc
                m["last_leave_time"] = None

            if row["first_join_at"]:
                fj = _parse_dt(row["first_join_at"])
                if fj and (m["first_join"] is None or fj < m["first_join"]):
                    m["first_join"] = fj
            if row["last_activity_at"] and m["status"] != "online":
                la = _parse_dt(row["last_activity_at"])
                if la and (m["last_activity"] is None or la > m["last_activity"]):
                    m["last_activity"] = la

    # ── Final formatting ──
    for m in members.values():
        m["today_total_duration"] = _fmt_dur(int(m["today_total_seconds"]))
        m["first_join_display"] = _myt_short(m["first_join"].isoformat()) if m["first_join"] else ""
        m["last_activity_display"] = _myt_short(m["last_activity"].isoformat()) if m["last_activity"] else ""

        # Cleanup internal tracking fields
        for f in ["open_from_ps", "open_from_sl", "open_from_cms"]:
            m.pop(f, None)

        # Email fallback from zoom_participants
        if not m.get("email"):
            try:
                row = conn.execute(
                    "SELECT email FROM zoom_participants WHERE name = ? AND email IS NOT NULL AND email != '' ORDER BY action_time DESC LIMIT 1",
                    (m["standard_name"],),
                ).fetchone()
                if row:
                    m["email"] = row[0]
            except Exception:
                pass

    # ── Sort ──
    sorted_members = sorted(
        members.values(),
        key=lambda m: (0 if m["status"] == "online" else 1, -(m["today_total_seconds"] or 0)),
    )

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
            rows = conn.execute("SELECT raw_name, display_name, match_key, count_enabled, aliases FROM member_display WHERE tenant_id = ? AND deleted=0", (tenant_id,)).fetchall()
        else:
            rows = conn.execute("SELECT raw_name, display_name, match_key, count_enabled, aliases FROM member_display WHERE deleted=0").fetchall()
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
    # 1. Match on match_key (lowercase, no spaces) — 优先级最高
    key = re.sub(r'\s+', '', name.lower())
    for raw, m in mapping.items():
        mk = m["key"]
        if mk and mk == key and not m["display"].endswith(" (2)"):
            return {"display_name": m["display"], "count_enabled": m["enabled"], "raw_name": name}

    # 2. Aliases match — 如果 raw_name 被某个主记录声明为别名，主记录优先
    name_lower = name.lower().replace(" ", "")
    primary_hit = None
    for raw, m in mapping.items():
        if m["display"].endswith(" (2)"):
            continue
        aliases = m.get("aliases", []) or []
        if name_lower in [a.lower().replace(" ", "") for a in aliases]:
            primary_hit = {"display_name": m["display"], "count_enabled": m["enabled"], "raw_name": name}
            break
    if primary_hit:
        return primary_hit

    # 3. Exact match on raw_name — 最后才用，避免 (2) 变体覆盖主记录
    if name in mapping:
        m = mapping[name]
        if not m["display"].endswith(" (2)"):
            return {"display_name": m["display"], "count_enabled": m["enabled"], "raw_name": name}
    # 2b. Fallback: match_key is empty, try display_name.lower() or raw_name.lower()
    for raw, m in mapping.items():
        mk = m["key"]
        if not mk:
            if re.sub(r'\s+', '', raw.lower()) == key or re.sub(r'\s+', '', m["display"].lower()) == key:
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
    {"event_type": "online_timeout_alert",                       "title": "连续在线超时预警（3小时）", "enabled": 1},
]


def seed_telegram_rules(tenant_id: str = "default"):
    """为指定租户插入默认 Telegram 告警规则，INSERT OR IGNORE 防止重复"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    for rule in DEFAULT_TELEGRAM_RULES:
        _cd = 300 if rule["event_type"] in ("participant_joined", "participant_left") else 60
        conn.execute(
            "INSERT OR IGNORE INTO telegram_alert_rules "
            "(tenant_id, event_type, title, enabled, cooldown_seconds, quiet_enabled, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, 0, ?, ?)",
            (tenant_id, rule["event_type"], rule["title"], rule["enabled"], _cd, now, now),
        )
    conn.commit()
    print(f"Seeded {len(DEFAULT_TELEGRAM_RULES)} rules for tenant '{tenant_id}'")


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


def upsert_telegram_rule(tenant_id: str, event_type: str, data: dict) -> int:
    """插入或更新告警规则（按租户隔离），返回 id"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()

    # 检查是否已存在（按租户+事件）
    existing = conn.execute(
        "SELECT id FROM telegram_alert_rules WHERE tenant_id = ? AND event_type = ?",
        (tenant_id, event_type),
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
        values.append(tenant_id)
        values.append(event_type)
        conn.execute(
            f"UPDATE telegram_alert_rules SET {', '.join(fields)} WHERE tenant_id = ? AND event_type = ?",
            values,
        )
        conn.commit()
        log_audit("update", "telegram_alert_rule", existing[0],
                  f"Updated rule for {event_type} (tenant={tenant_id})")
        return existing[0]
    else:
        cur = conn.execute(
            "INSERT INTO telegram_alert_rules "
            "(tenant_id, event_type, title, enabled, cooldown_seconds, "
            " quiet_enabled, quiet_start, quiet_end, target_channel_id, "
            " created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                tenant_id,
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
                  f"Created rule for {event_type} (tenant={tenant_id})")
        return cur.lastrowid


def delete_telegram_rule(tenant_id: str, event_type: str) -> bool:
    """删除指定租户+event_type 的告警规则，返回是否成功删除"""
    conn = _get_conn()
    existing = conn.execute(
        "SELECT id FROM telegram_alert_rules WHERE tenant_id = ? AND event_type = ?",
        (tenant_id, event_type),
    ).fetchone()
    if not existing:
        return False
    conn.execute(
        "DELETE FROM telegram_alert_rules WHERE tenant_id = ? AND event_type = ?",
        (tenant_id, event_type),
    )
    conn.commit()
    log_audit("delete", "telegram_alert_rule", existing[0],
              f"Deleted rule for {event_type} (tenant={tenant_id})")
    return True


def update_rule_test_result(tenant_id: str, event_type: str, ok: bool, error: str = ""):
    """更新规则测试结果（按租户隔离）"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE telegram_alert_rules SET last_test_at = ?, last_test_result = ?, last_test_error = ? WHERE tenant_id = ? AND event_type = ?",
        (now, 1 if ok else 0, error, tenant_id, event_type),
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
    0. 硬拦截：breakout/主会切换事件一律不推
    1. 查规则，不存在则返回 True（兼容旧逻辑）
    2. not enabled → False
    3. cooldown: 查 alert_sent 表同一 event_type 最近一次发送时间
    4. quiet_hours: 判断当前 MYT 时间是否在静默时段内
    """
    # 0. 硬拦截：breakout/主会切换事件不推送
    if event_type in ("participant_joined_breakout_room", "participant_left_breakout_room", "breakout_room_joined", "breakout_room_left"):
        return False

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


def audit_log_action(tenant_id: str = "default", action: str = "",
                     entity_type: str = "user", entity_id: int = None,
                     details: str = ""):
    """Write audit log entry with tenant context."""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO audit_logs (tenant_id, action, entity_type, entity_id, details, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (tenant_id, action, entity_type, entity_id, details, now),
    )
    conn.commit()


def resolve_ip_location(ip: str) -> str:
    """查 IP 地区（中文），走 ip_cache，未命中则查 ipapi.co"""
    if not ip:
        return ""
    # 内网/本地
    if ip in ("127.0.0.1", "::1", "localhost") or ip.startswith(("10.", "172.16.", "172.17.", "172.18.", "172.19.",
        "172.20.", "172.21.", "172.22.", "172.23.", "172.24.", "172.25.", "172.26.",
        "172.27.", "172.28.", "172.29.", "172.30.", "172.31.", "192.168.")):
        return "内网"
    conn = _get_conn()
    row = conn.execute("SELECT location FROM ip_cache WHERE ip=?", (ip,)).fetchone()
    if row and row[0]:
        return row[0]
    try:
        import urllib.request, json
        # ipapi.co — 返回中文国家/地区字段
        req = urllib.request.urlopen(f"https://ipapi.co/{ip}/json/", timeout=5)
        data = json.loads(req.read().decode())
        if data.get("error"):
            conn.execute("INSERT OR REPLACE INTO ip_cache (ip, location, updated_at) VALUES (?, '未知', datetime('now'))", (ip,))
            conn.commit()
            return "未知"
        parts = [data.get("country_name", "")]
        region = data.get("region", "")
        city = data.get("city", "")
        if region and region != data.get("country_name", ""):
            parts.append(region)
        if city and city != region:
            parts.append(city)
        location = " ".join(p for p in parts if p)
        conn.execute(
            "INSERT OR REPLACE INTO ip_cache (ip, location, updated_at) VALUES (?, ?, datetime('now'))",
            (ip, location))
        conn.commit()
        return location
    except Exception as e:
        # fallback to ipinfo.io
        try:
            import urllib.request, json
            req = urllib.request.urlopen(f"https://ipinfo.io/{ip}/json", timeout=5)
            data = json.loads(req.read().decode())
            loc = data.get("country", "")
            region = data.get("region", "")
            city = data.get("city", "")
            parts = [p for p in [loc, region, city] if p]
            location = " ".join(parts)
            conn.execute(
                "INSERT OR REPLACE INTO ip_cache (ip, location, updated_at) VALUES (?, ?, datetime('now'))",
                (ip, location))
            conn.commit()
            return location
        except Exception:
            return ""


def get_security_audit_logs(limit: int = 50) -> list[dict]:
    """获取最近的登录审计记录"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM security_audit_logs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["created_at_display"] = to_myt_str(d.get("created_at", ""))
        d["ip_location"] = resolve_ip_location(d.get("ip", ""))
        result.append(d)
    return result


def get_operation_audit_logs(limit: int = 50) -> list[dict]:
    """获取最近的操作审计记录"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        d["created_at_display"] = to_myt_str(d.get("created_at", ""))
        result.append(d)
    return result


def write_security_audit_log(username: str, action: str, ip: str = "",
                              user_agent: str = "", result: str = "success",
                              details: str = "", user_id: int = None,
                              tenant_id: str = ""):
    """写入安全审计日志（登录相关）"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    conn.execute(
        "INSERT INTO security_audit_logs (user_id, username, tenant_id, action, ip, "
        "user_agent, result, details, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, tenant_id, action, ip, user_agent, result, details, now),
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


def seed_member_groups(target_tenant_id: str | None = None):
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    if target_tenant_id:
        tenants_to_seed = [target_tenant_id]
    else:
        # 默认只为 default 租户 seed
        tenants_to_seed = ["default"]
    for g in DEFAULT_MEMBER_GROUPS:
        for tid in tenants_to_seed:
            conn.execute(
                "INSERT OR IGNORE INTO member_groups (name, description, tenant_id, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                (g["name"], g["description"], tid, now, now),
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


def add_member_to_group(group_id: int, member_name: str, tenant_id: str | None = None) -> bool:
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    try:
        # 校验 group_id 的租户归属
        if tenant_id:
            group = conn.execute(
                "SELECT tenant_id FROM member_groups WHERE id = ?", (group_id,)
            ).fetchone()
            if group and group[0] != tenant_id:
                log_audit("reject", "member_group_member", group_id,
                          f"跨租户拒绝: group租户{group[0]}!=成员租户{tenant_id}, member={member_name}")
                return False
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
        # 先删旧记录，防止旧表多记录导致迁移再读时混淆
        conn.execute(
            "DELETE FROM member_group_members WHERE member_name = ? AND group_id != ?",
            (member_name.strip(), group_id),
        )
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
    return re.sub(r"[\\s\\-\\._']+", "", (name or "").strip().lower())


# ── identity stability analysis (direct from zoom_events) ──

def _parse_participant_from_payload(payload_json):
    """从 zoom_events payload JSON 解析 participant 字段"""
    try:
        import json
        p = json.loads(payload_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        return None
    # zoom_events.payload stores the raw webhook JSON, which has
    # {"payload": {"object": {"participant": {...}}}, "event": "...", "event_ts": "..."}
    inner = p.get("payload", p)
    obj = inner.get("object", {}) if isinstance(inner, dict) else {}
    part = obj.get("participant", {}) or {}
    if not part.get("user_name"):
        return None
    return {
        "raw_name": part.get("user_name", ""),
        "public_ip": part.get("public_ip", "") or "",
        "user_id": part.get("user_id", "") or "",
        "participant_uuid": part.get("participant_uuid", "") or "",
        "email": part.get("email", "") or "",
        "join_time": part.get("join_time", "") or "",
        "event_time": None,
    }


def get_identity_stability(tenant_id: str, member_key: str, days: int = 30):
    """从 zoom_events 直接查询成员身份稳定性数据，不依赖中间表"""
    import json
    from collections import defaultdict
    conn = _get_conn()

    rows = conn.execute("""
        SELECT payload, event_type, created_at
        FROM zoom_events
        WHERE tenant_id = ?
          AND event_type IN ('meeting.participant_joined', 'meeting.participant_left')
          AND created_at >= datetime('now', ?)
        ORDER BY created_at ASC
    """, (tenant_id, f'-{days} days')).fetchall()

    name_variants = []  # list of {raw_name, count, last_seen}
    seen_names = {}
    ip_counter = defaultdict(int)
    last_ip = ""
    user_ids = set()
    participant_uuids = set()
    emails = set()
    sessions = []
    session_dates = set()
    first_joins = []
    last_leaves = []
    durations = []

    for r in rows:
        part = _parse_participant_from_payload(r["payload"])
        if not part:
            continue
        raw_name = part["raw_name"]
        dn, mk = normalize_member_name(raw_name)
        if mk != member_key:
            continue

        event_time = r["created_at"] or ""
        event_date = event_time[:10] if event_time else ""
        part["event_time"] = event_time

        # name variants
        if raw_name not in seen_names:
            seen_names[raw_name] = {"raw_name": raw_name, "count": 0, "last_seen": ""}
        seen_names[raw_name]["count"] += 1
        if event_time > seen_names[raw_name]["last_seen"]:
            seen_names[raw_name]["last_seen"] = event_time

        # IP
        ip = part["public_ip"]
        if ip:
            ip_counter[ip] += 1
            last_ip = ip

        # zoom IDs
        if part["user_id"]:
            user_ids.add(part["user_id"])
        if part["participant_uuid"]:
            participant_uuids.add(part["participant_uuid"])
        if part["email"]:
            emails.add(part["email"])

        # join time & duration (from joined events)
        jt = part.get("join_time", "")
        if jt and "T" in jt:
            time_part = jt.split("T")[1][:5]
            first_joins.append(time_part)

        # duration: look for paired leave event
        leave_time = ""
        if r["event_type"] == "meeting.participant_left":
            leave_part = _parse_participant_from_payload(r["payload"])
            if leave_part and leave_part.get("join_time"):
                lt = leave_part["join_time"]
                if lt and "T" in lt:
                    last_leaves.append(lt.split("T")[1][:5])
                    leave_time = lt

        sessions.append({
            "date": event_date,
            "join_time": jt,
            "leave_time": leave_time,
            "duration_minutes": 0,
        })
        if event_date:
            session_dates.add(event_date)

    total_ips = sum(ip_counter.values())
    main_ip = max(ip_counter, key=ip_counter.get) if ip_counter else ""
    ip_list = sorted(
        [{"ip": k, "count": v, "pct": round(v / total_ips * 100, 1) if total_ips else 0}
         for k, v in ip_counter.items()],
        key=lambda x: -x["count"]
    )

    # duration: estimate from session min/max per date
    date_session_ranges = defaultdict(lambda: {"first": "", "last": ""})
    for s in sessions:
        d = s["date"]
        if s["join_time"] and (not date_session_ranges[d]["first"] or s["join_time"] < date_session_ranges[d]["first"]):
            date_session_ranges[d]["first"] = s["join_time"]
        if s["leave_time"] and s["leave_time"] > date_session_ranges[d]["last"]:
            date_session_ranges[d]["last"] = s["leave_time"]

    for d, rng in date_session_ranges.items():
        if rng["first"] and rng["last"]:
            try:
                fh, fm = rng["first"].split("T")[1][:5].split(":")
                lh, lm = rng["last"].split("T")[1][:5].split(":")
                dur = (int(lh) * 60 + int(lm)) - (int(fh) * 60 + int(fm))
                if dur > 0:
                    durations.append(dur)
            except (ValueError, IndexError):
                pass

    return {
        "name_variants": sorted(seen_names.values(), key=lambda x: -x["count"]),
        "ip_summary": {
            "ip_list": ip_list,
            "main_ip": main_ip,
            "main_ip_pct": round(ip_counter.get(main_ip, 0) / total_ips * 100, 1) if total_ips else 0,
            "last_ip": last_ip,
            "unique_ip_count": len(ip_counter),
        },
        "zoom_ids": {
            "user_ids": sorted(user_ids),
            "participant_uuids": sorted(list(participant_uuids))[:50],
            "emails": sorted(emails),
        },
        "sessions": sessions,
        "total_sessions": len(sessions),
        "attendance_summary": {
            "total_days": len(session_dates),
            "avg_first_join": _avg_time(first_joins) if first_joins else "",
            "avg_last_leave": _avg_time(last_leaves) if last_leaves else "",
            "avg_duration_minutes": round(sum(durations) / len(durations), 1) if durations else 0,
        },
    }


def _avg_time(times):
    """计算 HH:MM 列表的平均时间"""
    total_sec = 0
    for t in times:
        parts = t.split(":")
        if len(parts) >= 2:
            total_sec += int(parts[0]) * 3600 + int(parts[1]) * 60
    if not times:
        return ""
    avg_sec = total_sec // len(times)
    return f"{avg_sec // 3600:02d}:{(avg_sec % 3600) // 60:02d}"


def find_similar_members(tenant_id: str, member_key: str, days: int = 30):
    """寻找与目标成员相似的其他成员（只推荐，不自动合并）"""
    from collections import defaultdict
    import json

    target = get_identity_stability(tenant_id, member_key, days)
    if not target or target["total_sessions"] == 0:
        return []

    conn = _get_conn()

    # Get all unique member_keys from zoom_events
    all_rows = conn.execute("""
        SELECT payload, created_at FROM zoom_events
        WHERE tenant_id = ?
          AND event_type IN ('meeting.participant_joined', 'meeting.participant_left')
          AND created_at >= datetime('now', ?)
        ORDER BY created_at ASC
    """, (tenant_id, f'-{days} days')).fetchall()

    # Group raw data by member_key
    member_data = defaultdict(lambda: {
        "ips": set(), "session_dates": set(), "durations": [],
        "first_joins": [],
    })

    for r in all_rows:
        part = _parse_participant_from_payload(r["payload"])
        if not part:
            continue
        raw_name = part["raw_name"]
        dn, mk = normalize_member_name(raw_name)
        if mk == member_key:
            continue

        md = member_data[mk]
        if part["public_ip"]:
            md["ips"].add(part["public_ip"])
        md["session_dates"].add(r["created_at"][:10] if r["created_at"] else "")

    target_ips = set()
    for ip_info in target["ip_summary"]["ip_list"]:
        if ip_info["ip"]:
            target_ips.add(ip_info["ip"])
    target_dates = set(s["date"] for s in target["sessions"] if s["date"])
    target_avg_dur = target["attendance_summary"]["avg_duration_minutes"]

    results = []
    for mk, md in member_data.items():
        score = 0
        reasons = []

        # 1. Name similarity (Levenshtein)
        if len(member_key) >= 3 and len(mk) >= 3:
            dist = _levenshtein_distance(member_key, mk)
            max_len = max(len(member_key), len(mk))
            if max_len > 0:
                sim = 1 - (dist / max_len)
                if sim > 0.5:
                    score += 20
                    reasons.append("名称相似")

        # 2. Shared IP
        common_ips = target_ips & md["ips"]
        if common_ips:
            score += 40
            reasons.append("主IP相同")

        # 3. Never co-present
        if target_dates.isdisjoint(md["session_dates"]):
            score += 20
            reasons.append("从未同时在线")

        if score >= 20:
            results.append({
                "member_key": mk,
                "score": min(score, 100),
                "reasons": reasons,
            })

    results.sort(key=lambda x: -x["score"])
    return results[:10]


def _levenshtein_distance(s1: str, s2: str) -> int:
    if len(s1) < len(s2):
        return _levenshtein_distance(s2, s1)
    if len(s2) == 0:
        return len(s1)
    prev_row = list(range(len(s2) + 1))
    for i, c1 in enumerate(s1):
        curr_row = [i + 1]
        for j, c2 in enumerate(s2):
            insertions = prev_row[j + 1] + 1
            deletions = curr_row[j] + 1
            substitutions = prev_row[j] + (c1 != c2)
            curr_row.append(min(insertions, deletions, substitutions))
        prev_row = curr_row
    return prev_row[-1]


import secrets
import hashlib
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
                role: str = "user", tenant_id: str = "default") -> int:
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


def get_users(user_role: str = None, tenant_id: str = None, viewer_role: str = None) -> list[dict]:
    """Get users with role-based filtering.
    super_admin: all users
    admin: all except super_admin
    tenant_admin: users in own tenant with role user
    user: no access (empty list)
    """
    conn = _get_conn()
    if viewer_role == "super_admin":
        rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    elif viewer_role == "admin":
        rows = conn.execute(
            "SELECT * FROM users WHERE role != 'super_admin' ORDER BY id"
        ).fetchall()
    elif viewer_role == "tenant_admin" and tenant_id:
        rows = conn.execute(
            "SELECT * FROM users WHERE tenant_id = ? AND role = 'user' ORDER BY id",
            (tenant_id,),
        ).fetchall()
    else:
        return []
    return [dict(r) for r in rows]


def verify_user_password(username: str, password: str) -> dict | None:
    """Verify user credentials. Returns user dict on success, None on failure.
    NOTE: inactive users are still returned — caller must check is_active."""
    user = get_user_by_username(username)
    if not user:
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
        "SELECT * FROM tenants WHERE is_active = 1 ORDER BY created_at DESC"
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


# ── Tenant Detail / Admin Stats ──────────────────────────────────────────

def get_tenant_by_id(tenant_id: str) -> dict | None:
    """获取单个租户完整信息。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    return dict(row) if row else None


def get_all_tenants_with_inactive() -> list[dict]:
    """获取所有租户（含停用），用于管理列表。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM tenants ORDER BY created_at DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def count_users_by_tenant(tenant_id: str) -> int:
    """用户数。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM users WHERE tenant_id = ? AND is_active = 1",
        (tenant_id,)
    ).fetchone()
    return row["c"] if row else 0


def count_zoom_accounts_by_tenant(tenant_id: str) -> int:
    """Zoom 账号数。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM zoom_accounts WHERE tenant_id = ?",
        (tenant_id,)
    ).fetchone()
    return row["c"] if row else 0


def count_telegram_channels_by_tenant(tenant_id: str) -> int:
    """频道数（从 tenant_channels 或 telegram_channels 表统计）。"""
    conn = _get_conn()
    # Try tenant_channels first (newer), fallback to telegram_channels
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM tenant_channels WHERE tenant_id = ?",
        (tenant_id,)
    ).fetchone()
    if row and row["c"] > 0:
        return row["c"]
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM telegram_channels WHERE tenant_id = ?",
        (tenant_id,)
    ).fetchone()
    return row["c"] if row else 0


def count_members_by_tenant(tenant_id: str) -> int:
    """分组成员数。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM member_group_members WHERE tenant_id = ?",
        (tenant_id,)
    ).fetchone()
    return row["c"] if row else 0


def count_alert_rules_by_tenant(tenant_id: str) -> int:
    """告警规则数。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM telegram_alert_rules WHERE tenant_id = ?",
        (tenant_id,)
    ).fetchone()
    return row["c"] if row else 0


def count_enabled_alerts_by_tenant(tenant_id: str) -> int:
    """启用的告警规则数。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM telegram_alert_rules WHERE tenant_id = ? AND enabled = 1",
        (tenant_id,)
    ).fetchone()
    return row["c"] if row else 0


def count_groups_by_tenant(tenant_id: str) -> int:
    """分组数。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM member_groups WHERE tenant_id = ?",
        (tenant_id,)
    ).fetchone()
    return row["c"] if row else 0


def get_tenant_zoom_accounts(tenant_id: str) -> list[dict]:
    """带状态的 Zoom 账号列表。"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM zoom_accounts WHERE tenant_id = ? ORDER BY created_at DESC",
        (tenant_id,)
    ).fetchall()
    return [dict(r) for r in rows]


def update_tenant(tenant_id: str, **kwargs) -> bool:
    """更新租户信息。"""
    allowed = {"display_name", "name", "plan", "is_active", "telegram_bot_token",
               "telegram_bot_username", "zoom_plan", "live_mode", "sharing_mode",
               "report_mode", "metrics_available", "reports_available"}
    updates = {k: v for k, v in kwargs.items() if k in allowed}
    if not updates:
        return False
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    clauses = ", ".join(f"{k} = ?" for k in updates)
    values = list(updates.values()) + [now, tenant_id]
    conn = _get_conn()
    conn.execute(
        f"UPDATE tenants SET {clauses}, updated_at = ? WHERE id = ?",
        values,
    )
    conn.commit()
    return True


def get_tenant_channels_count(tenant_id: str) -> int:
    """获取租户的频道数（按 tenant_channels 统计）。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM tenant_channels WHERE tenant_id = ?",
        (tenant_id,)
    ).fetchone()
    return row["c"] if row else 0


def get_tenant_bot_status(tenant_id: str) -> dict:
    """获取租户 bot 状态。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT telegram_bot_token, telegram_bot_username, telegram_bot_verified_at "
        "FROM tenants WHERE id = ?", (tenant_id,)
    ).fetchone()
    if not row:
        return {"has_bot": False, "username": "", "verified": False}
    has_bot = bool(row[0])
    return {
        "has_bot": has_bot,
        "username": row[1] or "",
        "verified": bool(row[2]),
        "verified_at": row[2] or "",
    }


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


def update_user_full(target_id: int, **kwargs) -> bool:
    """Update any fields on users table. Accept username, display_name, role,
    tenant_id, is_active, telegram_chat_id, etc. Safely filters to valid columns."""
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    valid_cols = {row[1] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
    invalid = [k for k in kwargs if k not in valid_cols]
    if invalid:
        raise ValueError(f"Invalid columns: {invalid}")

    fields = ["updated_at = ?"]
    vals = [now]
    for key, val in kwargs.items():
        if key in ("id", "password_hash", "updated_at", "created_at"):
            continue  # protect immutable/auto fields
        if val is not None:
            fields.append(f"{key} = ?")
            vals.append(val)
    if len(fields) == 1:
        return False  # nothing to update
    vals.append(target_id)
    conn.execute(
        f"UPDATE users SET {', '.join(fields)} WHERE id = ?",
        vals,
    )
    conn.commit()
    return True


def reset_user_password(target_id: int, new_password: str) -> bool:
    """Reset user password with proper hashing."""
    conn = _get_conn()
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    pw_hash = _hash_pw(new_password)
    conn.execute(
        "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
        (pw_hash, now, target_id),
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


def get_meeting_history(tenant_id: str, limit: int = 50, offset: int = 0,
                        show_test: bool = False) -> tuple:
    """获取历史会议列表（按 meeting_id 聚合），仅统计 webhook 的 enter/leave 事件

    参数:
        show_test: True 时包含 test_/TEST/concurrent_test 开头的测试会议
    """
    conn = _get_conn()
    rows = conn.execute(
        "SELECT meeting_id, name, email, action, action_time "
        "FROM zoom_participants "
        "WHERE tenant_id = ? AND source = 'webhook' AND action IN ('enter','joined','leave','left') "
        "ORDER BY meeting_id, action_time",
        (tenant_id,),
    ).fetchall()
    meetings = {}
    last_key = None
    last_action = None
    for r in rows:
        mid = r["meeting_id"]
        name = r["name"]
        action = r["action"]
        action_time = r["action_time"]

        # 跳过测试会议（除非 show_test=True）
        if not show_test and (mid.lower().startswith("test_") or mid.upper().startswith("TEST") or mid.lower().startswith("concurrent_test_")):
            continue

        # 标准化 action
        if action in ("joined",):
            action = "enter"
        elif action in ("left",):
            action = "leave"

        # 同 meeting + 同 name + 同秒 + 同 action → 去重
        cur_key = (mid, name, action_time)
        if cur_key == last_key and action == last_action:
            continue
        last_key, last_action = cur_key, action

        if mid not in meetings:
            meetings[mid] = {
                "meeting_id": mid,
                "first_event": action_time,
                "last_event": action_time,
                "participants": {},
                "total_events": 0,
            }
        m = meetings[mid]
        try:
            email = r["email"]
        except (KeyError, IndexError):
            email = ""
        if name not in m["participants"]:
            m["participants"][name] = {
                "name": name,
                "email": email,
                "first_seen": action_time,
                "last_seen": action_time,
                "enter_count": 0,
                "leave_count": 0,
                "other_count": 0,
            }
        p = m["participants"][name]
        if action_time < p["first_seen"]:
            p["first_seen"] = action_time
        if action_time > p["last_seen"]:
            p["last_seen"] = action_time
        if action == "enter":
            p["enter_count"] += 1
        elif action == "leave":
            p["leave_count"] += 1
        else:
            p["other_count"] += 1
        m["total_events"] += 1
        if action_time < m["first_event"]:
            m["first_event"] = action_time
        if action_time > m["last_event"]:
            m["last_event"] = action_time
    for mid in meetings:
        m = meetings[mid]
        topic = conn.execute(
            "SELECT topic FROM meeting_topics WHERE meeting_id = ? LIMIT 1", (mid,)
        ).fetchone()
        m["topic"] = topic[0] if topic else mid
        m["participant_count"] = len(m["participants"])
        participants_list = sorted(m["participants"].values(), key=lambda p: p["first_seen"])
        for p in participants_list:
            p["first_seen_display"] = _myt_short(p["first_seen"])
            p["last_seen_display"] = _myt_short(p["last_seen"])
        m["participants"] = participants_list
        try:
            f = datetime.fromisoformat(m["first_event"].replace("Z", "+00:00"))
            l = datetime.fromisoformat(m["last_event"].replace("Z", "+00:00"))
            dur = int((l - f).total_seconds())
            m["duration_seconds"] = dur
            m["duration_display"] = _fmt_dur(dur)
        except Exception:
            m["duration_seconds"] = 0
            m["duration_display"] = "—"
    sorted_list = sorted(meetings.values(), key=lambda m: m["last_event"], reverse=True)
    total = len(sorted_list)
    page = sorted_list[offset:offset + limit]
    for m in page:
        m["first_event_display"] = _myt_short(m["first_event"])
        m["last_event_display"] = _myt_short(m["last_event"])
    return page, total


def get_sharing_records(tenant_id: str, limit: int = 50,
                        start_time: str | None = None,
                        end_time: str | None = None,
                        search: str | None = None,
                        group_id: str | None = None) -> tuple:
    """
获取共享...[truncated]
    start_time/end_time: UTC ISO 字符串，按 start_time 过滤（MYT 时区的起止由前端计算传入）
    search: 按 user_name 模糊搜索
    """
    conn = _get_conn()
    from datetime import datetime, timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    now_myt = datetime.now(timezone.utc).astimezone(MYT)
    today_start_utc = (now_myt - timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end_utc = today_start_utc + timedelta(days=1)
    stale_threshold_utc = (now_myt - timedelta(hours=6)).astimezone(timezone.utc)

    # 构建 WHERE 条件
    where_clauses = ["sl.tenant_id = ?"]
    where_params = [tenant_id]
    if start_time:
        where_clauses.append("sl.start_time >= ?")
        where_params.append(start_time)
    if end_time:
        where_clauses.append("sl.start_time < ?")
        where_params.append(end_time)
    if search:
        where_clauses.append("sl.user_name LIKE ?")
        where_params.append(f"%{search}%")
    if group_id:
        where_clauses.append("md.group_id = ?")
        where_params.append(group_id)

    sql = f"SELECT sl.*, COALESCE(mg.name, '') AS group_name, COALESCE(md.group_id, '') AS group_id FROM sharing_live sl LEFT JOIN member_display md ON (md.raw_name=sl.user_name OR md.display_name=sl.user_name) AND md.tenant_id=sl.tenant_id LEFT JOIN member_groups mg ON mg.id=md.group_id AND mg.tenant_id=md.tenant_id WHERE {' AND '.join(where_clauses)} ORDER BY sl.start_time DESC LIMIT ?"
    where_params_str = [str(p) for p in where_params] + [str(limit)]
    rows = conn.execute(sql, where_params_str).fetchall()
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


def get_participants_by_meeting(meeting_id: str) -> dict:
    """获取指定会议的详细统计信息"""
    conn = _get_conn()
    rows = conn.execute("""
        SELECT DISTINCT name, email
        FROM zoom_participants
        WHERE meeting_id = ?
        ORDER BY name
    """, (meeting_id,)).fetchall()
    # 总参与人数
    total_row = conn.execute(
        "SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE meeting_id = ?",
        (meeting_id,)
    ).fetchone()
    total_participants = total_row[0] if total_row else 0
    # 当前在线人数（最后动作是 enter 且没有后续 leave）
    rows_online = conn.execute("""
        SELECT DISTINCT name FROM zoom_participants zp
        WHERE meeting_id = ? AND action = 'enter'
        AND NOT EXISTS (
            SELECT 1 FROM zoom_participants zp2
            WHERE zp2.meeting_id = zp.meeting_id
            AND zp2.name = zp.name
            AND zp2.action IN ('leave', 'left')
            AND zp2.action_time > zp.action_time
        )
    """, (meeting_id,)).fetchall()
    online_count = len(rows_online)
    # 最近进入成员（前5个）
    recent_joins = conn.execute("""
        SELECT name, action_time FROM zoom_participants
        WHERE meeting_id = ? AND action IN ('enter', 'joined')
        ORDER BY action_time DESC LIMIT 5
    """, (meeting_id,)).fetchall()
    from datetime import datetime, timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    def _short(utc_str):
        if not utc_str: return "—"
        try:
            s = utc_str.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            return dt.astimezone(MYT).strftime("%m-%d %H:%M")
        except:
            return "—"
    recent_join_list = [{"name": r["name"], "time": _short(r["action_time"])} for r in recent_joins]
    # 最近离开成员（前5个）
    recent_leaves = conn.execute("""
        SELECT name, action_time FROM zoom_participants
        WHERE meeting_id = ? AND action IN ('leave', 'left')
        ORDER BY action_time DESC LIMIT 5
    """, (meeting_id,)).fetchall()
    recent_leave_list = [{"name": r["name"], "time": _short(r["action_time"])} for r in recent_leaves]
    # 会议开始时间
    first_row = conn.execute(
        "SELECT MIN(action_time) FROM zoom_participants WHERE meeting_id = ?",
        (meeting_id,)
    ).fetchone()
    meeting_start = _short(first_row[0]) if first_row and first_row[0] else "—"
    return {
        "meeting_id": meeting_id,
        "total_participants": total_participants,
        "online_count": online_count,
        "participant_list": [dict(r) for r in rows],
        "recent_joins": recent_join_list,
        "recent_leaves": recent_leave_list,
        "meeting_start": meeting_start,
    }


# ═══════════════════════════════════════════
# Security - Login Attempts / Rate Limiting
# ═══════════════════════════════════════════

def check_login_attempts(ip: str) -> dict:
    """Check if IP is locked. Returns {'locked': bool, 'remaining': int, 'locked_until': str or None}"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT failed_count, locked_until FROM login_attempts WHERE ip = ? ORDER BY id DESC LIMIT 1",
        (ip,)
    ).fetchone()
    if not row:
        return {"locked": False, "remaining": 5, "locked_until": None}
    locked_until = row["locked_until"]
    if locked_until:
        from datetime import datetime, timezone
        try:
            until = datetime.fromisoformat(locked_until.replace("Z", "+00:00"))
            if datetime.now(timezone.utc) < until:
                return {"locked": True, "remaining": 0, "locked_until": locked_until}
        except:
            pass
    return {"locked": False, "remaining": max(0, 5 - row["failed_count"]), "locked_until": None}

def record_login_attempt(ip: str, username: str, success: bool) -> None:
    """Record login attempt. On success, clear the record. On failure, increment."""
    conn = _get_conn()
    if success:
        conn.execute("DELETE FROM login_attempts WHERE ip = ?", (ip,))
        conn.commit()
        return
    from datetime import datetime, timezone, timedelta
    row = conn.execute(
        "SELECT id, failed_count FROM login_attempts WHERE ip = ? ORDER BY id DESC LIMIT 1",
        (ip,)
    ).fetchone()
    now = datetime.now(timezone.utc)
    if row:
        new_count = row["failed_count"] + 1
        if new_count >= 5:
            locked_until = (now + timedelta(minutes=15)).isoformat()
            conn.execute(
                "UPDATE login_attempts SET failed_count = ?, locked_until = ?, created_at = ? WHERE id = ?",
                (new_count, locked_until, now.isoformat(), row["id"])
            )
        else:
            conn.execute(
                "UPDATE login_attempts SET failed_count = ?, created_at = ? WHERE id = ?",
                (new_count, now.isoformat(), row["id"])
            )
    else:
        conn.execute(
            "INSERT INTO login_attempts (ip, username, failed_count) VALUES (?, ?, 1)",
            (ip, username)
        )
    conn.commit()


# ═══════════════════════════════════════════
# Security - Audit Logs
# ═══════════════════════════════════════════

def log_security_event(
    action: str,
    user_id: int = None,
    username: str = "",
    tenant_id: str = "",
    ip: str = "",
    user_agent: str = "",
    result: str = "success",
    details: str = "",
) -> int:
    """Record a security audit event."""
    conn = _get_conn()
    cur = conn.execute(
        "INSERT INTO security_audit_logs (user_id, username, tenant_id, action, ip, user_agent, result, details) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (user_id, username, tenant_id, action, ip, user_agent, result, details),
    )
    conn.commit()
    return cur.lastrowid


def get_last_login_ip(user_id: int) -> str | None:
    """返回该用户上一次 login_success 的 IP（非最新一条），若不存在返回 None。"""
    conn = _get_conn()
    row = conn.execute(
        "SELECT ip FROM security_audit_logs "
        "WHERE user_id = ? AND action = 'login_success' AND result = 'success' "
        "ORDER BY id DESC LIMIT 1 OFFSET 1",
        (user_id,)
    ).fetchone()
    if row and row["ip"]:
        return row["ip"]
    return None


# ═══════════════════════════════════════════
# Account Management
# ═══════════════════════════════════════════

# get_user_by_id and get_user_by_username already exist above
# import json used below — ensure it's available (db.py already has it at top for webhook)

def set_user_telegram_chat_id(user_id: int, chat_id: str) -> None:
    """Bind Telegram chat_id to user."""
    conn = _get_conn()
    conn.execute("UPDATE users SET telegram_chat_id = ? WHERE id = ?", (chat_id, user_id))
    conn.commit()

def enable_telegram_2fa(user_id: int) -> None:
    """Enable Telegram 2FA for user."""
    from datetime import datetime, timezone
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET telegram_2fa_enabled = 1, telegram_2fa_verified_at = ? WHERE id = ?",
        (datetime.now(timezone.utc).isoformat(), user_id)
    )
    conn.commit()

def disable_telegram_2fa(user_id: int) -> None:
    """Disable Telegram 2FA for user."""
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET telegram_2fa_enabled = 0, telegram_chat_id = '' WHERE id = ?",
        (user_id,)
    )
    conn.commit()

def is_2fa_enabled(user_id: int) -> bool:
    """Check if Telegram 2FA is enabled for user."""
    conn = _get_conn()
    row = conn.execute("SELECT telegram_2fa_enabled FROM users WHERE id = ?", (user_id,)).fetchone()
    return bool(row and row["telegram_2fa_enabled"])


# ═══════════════════════════════════════════
# Security - Backup Codes
# ═══════════════════════════════════════════

import secrets

def generate_backup_codes(count: int = 8) -> list[str]:
    """Generate N backup codes in XXXX-XXXX format. Returns plaintext list."""
    codes = []
    for _ in range(count):
        part1 = secrets.randbelow(10000)
        part2 = secrets.randbelow(10000)
        codes.append(f"{part1:04d}-{part2:04d}")
    return codes

def hash_backup_codes(codes: list[str]) -> str:
    """Hash backup codes for storage. Returns JSON list of SHA256 hex strings."""
    import hashlib
    hashed = [hashlib.sha256(c.encode()).hexdigest() for c in codes]
    return json.dumps(hashed)

def verify_backup_code(user_id: int, code: str) -> bool:
    """Verify and consume a backup code. Returns True if valid."""
    conn = _get_conn()
    row = conn.execute(
        "SELECT twofa_backup_codes FROM users WHERE id = ?", (user_id,)
    ).fetchone()
    if not row or not row["twofa_backup_codes"]:
        return False
    import hashlib
    stored = json.loads(row["twofa_backup_codes"])
    code_hash = hashlib.sha256(code.encode()).hexdigest()
    for i, h in enumerate(stored):
        if h == code_hash:
            stored.pop(i)
            conn.execute(
                "UPDATE users SET twofa_backup_codes = ? WHERE id = ?",
                (json.dumps(stored), user_id)
            )
            conn.commit()
            return True
    return False

def save_backup_codes(user_id: int, codes: list[str]) -> None:
    """Save hashed backup codes to user record."""
    conn = _get_conn()
    conn.execute(
        "UPDATE users SET twofa_backup_codes = ? WHERE id = ?",
        (hash_backup_codes(codes), user_id)
    )
    conn.commit()


# ── System Settings helpers ────────────────────────────────────────────────


def get_all_settings() -> dict[str, str]:
    """Get all settings as a flat dict."""
    conn = _get_conn()
    rows = conn.execute("SELECT key, value FROM settings").fetchall()
    return {r["key"]: r["value"] for r in rows}


# ── Admin Center Stats ────────────────────────────────────────────────────


def count_total_tenants() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM tenants WHERE is_active = 1").fetchone()
    return row["c"] if row else 0


def count_total_users() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM users WHERE is_active = 1").fetchone()
    return row["c"] if row else 0


def count_total_zoom_accounts() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM zoom_accounts WHERE is_active = 1").fetchone()
    return row["c"] if row else 0


def count_total_channels() -> int:
    conn = _get_conn()
    row = conn.execute("SELECT COUNT(*) AS c FROM telegram_channels WHERE enabled = 1").fetchone()
    return row["c"] if row else 0


def count_today_alerts() -> int:
    """Count alerts created today (UTC)."""
    conn = _get_conn()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM alerts WHERE created_at >= ?", (today,)
    ).fetchone()
    return row["c"] if row else 0


def count_today_push_count() -> int:
    """Count alert_sent entries from today (UTC)."""
    conn = _get_conn()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    row = conn.execute(
        "SELECT COUNT(*) AS c FROM alert_sent WHERE sent_at >= ?", (today,)
    ).fetchone()
    return row["c"] if row else 0

# ═══════════════════════════════════════════════════════════════
# 班次统计管理 (shift_assignments)
# ═══════════════════════════════════════════════════════════════

def get_shift_assignments(shift_date: str = None, tenant_id: str = None) -> list[dict]:
    """查询班次登记列表，支持按日期筛选。"""
    conn = _get_conn()
    where = []
    params = []
    if tenant_id:
        where.append("sa.tenant_id = ?")
        params.append(tenant_id)
    if shift_date:
        where.append("sa.shift_date = ?")
        params.append(shift_date)
    sql = "SELECT sa.* FROM shift_assignments sa"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY sa.member_name"
    rows = conn.execute(sql, params).fetchall()
    return [dict(r) for r in rows]


def create_shift_assignment(tenant_id: str, member_name: str, shift_name: str,
                            shift_date: str, shift_start: str, shift_end: str,
                            created_by: int = None) -> int:
    """新增班次登记。返回 id。"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    try:
        conn.execute(
            "INSERT INTO shift_assignments (tenant_id, member_name, shift_name, "
            "shift_date, shift_start, shift_end, created_by, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (tenant_id, member_name, shift_name, shift_date,
             shift_start, shift_end, created_by, now),
        )
        return conn.execute("SELECT last_insert_rowid()").fetchone()[0]
    except Exception as e:
        if "UNIQUE" in str(e):
            return None
        raise


def delete_shift_assignment(assignment_id: int, tenant_id: str = None) -> bool:
    """删除班次登记。"""
    conn = _get_conn()
    where = "id = ?"
    params = [assignment_id]
    if tenant_id:
        where += " AND tenant_id = ?"
        params.append(tenant_id)
    conn.execute(f"DELETE FROM shift_assignments WHERE {where}", params)
    return conn.total_changes > 0


def batch_create_shift_assignments(entries: list[dict]) -> tuple[int, int]:
    """批量登记班次。返回 (成功数, 失败数)"""
    conn = _get_conn()
    now = datetime.now(timezone.utc).isoformat()
    success = 0
    failed = 0
    for entry in entries:
        try:
            conn.execute(
                "INSERT OR IGNORE INTO shift_assignments "
                "(tenant_id, member_name, shift_name, shift_date, "
                "shift_start, shift_end, created_by, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (entry["tenant_id"], entry["member_name"], entry["shift_name"],
                 entry["shift_date"], entry["shift_start"], entry["shift_end"],
                 entry.get("created_by"), now),
            )
            if conn.total_changes > 0:
                success += 1
            else:
                failed += 1
        except Exception:
            failed += 1
    return success, failed


def get_shift_attendance_for_shift(
    tenant_id: str,
    shift_start_myt_str: str,
    shift_end_myt_str: str,
    member_names: list[str] = None,
) -> tuple[list[dict], dict]:
    """
    生成班次统计（session overlap 算法）。

    参数:
        tenant_id: 租户
        shift_start_myt_str: 班次开始时间 (MYT ISO) e.g. "2026-06-18T07:00:00"
        shift_end_myt_str:   班次结束时间 (MYT ISO) e.g. "2026-06-18T19:00:00"
        member_names: 可选，指定成员列表（来自 shift_assignments 已登记成员）

    返回:
        ([成员统计数据], {班次汇总信息})
    """
    from datetime import datetime, timezone, timedelta
    from collections import OrderedDict

    now_utc = datetime.now(timezone.utc)

    shift_start_myt = datetime.fromisoformat(shift_start_myt_str)
    shift_end_myt = datetime.fromisoformat(shift_end_myt_str)
    shift_start_utc = shift_start_myt - timedelta(hours=8)
    shift_end_utc = shift_end_myt - timedelta(hours=8)
    shift_start_utc = shift_start_utc.replace(tzinfo=timezone.utc)
    shift_end_utc = shift_end_utc.replace(tzinfo=timezone.utc)

    query_start_utc = shift_start_utc - timedelta(hours=12)
    query_start_str = query_start_utc.strftime("%Y-%m-%dT%H:%M:%S")
    query_end_str = (shift_end_utc + timedelta(hours=12)).strftime("%Y-%m-%dT%H:%M:%S")

    conn = _get_conn()
    if member_names:
        placeholders = ",".join("?" for _ in member_names)
        params = [query_start_str, query_end_str, tenant_id] + member_names
        rows = conn.execute(
            f"SELECT * FROM zoom_participants "
            f"WHERE action_time >= ? AND action_time < ? "
            f"AND tenant_id = ? AND name IN ({placeholders}) "
            f"ORDER BY name, action_time",
            params,
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM zoom_participants "
            "WHERE action_time >= ? AND action_time < ? AND tenant_id = ? "
            "ORDER BY name, action_time",
            (query_start_str, query_end_str, tenant_id),
        ).fetchall()

    all_events = [dict(r) for r in rows]

    meeting_start = None
    for ev in all_events:
        if ev["action"] in ("enter", "joined"):
            try:
                at_dt = datetime.fromisoformat(str(ev["action_time"]).replace("Z", "+00:00"))
            except:
                at_dt = datetime.fromisoformat(str(ev["action_time"]))
            if at_dt.tzinfo is None:
                at_dt = at_dt.replace(tzinfo=timezone.utc)
            if at_dt >= shift_start_utc:
                meeting_start = at_dt
                break
    if meeting_start is None:
        meeting_start = shift_start_utc

    effective_start = max(shift_start_utc, meeting_start)
    effective_end = min(shift_end_utc, now_utc)
    if effective_end < effective_start:
        effective_end = effective_start

    meeting_not_open_seconds = int((meeting_start - shift_start_utc).total_seconds()) if meeting_start > shift_start_utc else 0
    required_seconds = int((effective_end - effective_start).total_seconds())
    if required_seconds < 0:
        required_seconds = 0

    _group_map_cache = {}
    grp_rows = conn.execute(
        "SELECT DISTINCT md.raw_name, md.display_name, COALESCE(g.name, '') AS group_name "
        "FROM member_display md "
        "LEFT JOIN member_groups g ON g.id = md.group_id AND g.tenant_id = md.tenant_id "
        "WHERE md.tenant_id = ? AND md.group_id IS NOT NULL",
        (tenant_id,),
    ).fetchall()
    for gr in grp_rows:
        grd = dict(gr)
        for key in (grd.get("raw_name", "").strip().lower().replace(" ", ""),
                    grd.get("display_name", "").strip().lower().replace(" ", "")):
            if key:
                _group_map_cache[key] = grd.get("group_name", "")

    def _batch_group_lookup(name: str) -> str:
        key = name.strip().lower().replace(" ", "")
        return _group_map_cache.get(key, "")

    from db import resolve_display_name as _resolve_display_name

    members = OrderedDict()
    for e in all_events:
        resolved = _resolve_display_name(e["name"])
        display_name = resolved["display_name"]
        if display_name not in members:
            members[display_name] = {
                "standard_name": display_name,
                "raw_name": resolved.get("raw_name", display_name),
                "group_name": _batch_group_lookup(display_name),
                "raw_events": [],
            }
        action = e["action"]
        if action not in ("enter", "joined", "leave", "left"):
            continue
        members[display_name]["raw_events"].append({
            "action": action,
            "action_time": e["action_time"],
        })

    for m in members.values():
        m["raw_events"].sort(key=lambda x: x["action_time"])
        deduped = []
        for ev in m["raw_events"]:
            if deduped and deduped[-1]["action"] in ("enter", "joined", "leave", "left")                and deduped[-1]["action"] == ev["action"]:
                continue
            deduped.append(ev)

        online_seconds = 0
        away_seconds = 0
        max_away = 0
        away_over_15_count = 0
        is_online = False
        last_overlap_leave = None
        overlap_sessions = 0
        first_online_dt = None
        last_online_dt = None

        i = 0
        while i < len(deduped):
            ev = deduped[i]
            if ev["action"] in ("enter", "joined"):
                enter_dt = datetime.fromisoformat(ev["action_time"])
                if enter_dt.tzinfo is None:
                    enter_dt = enter_dt.replace(tzinfo=timezone.utc)
                leave_dt = None
                for j in range(i + 1, len(deduped)):
                    if deduped[j]["action"] in ("leave", "left"):
                        leave_dt = datetime.fromisoformat(deduped[j]["action_time"])
                        if leave_dt is not None and leave_dt.tzinfo is None:
                            leave_dt = leave_dt.replace(tzinfo=timezone.utc)
                        i = j
                        break

                session_start_raw = enter_dt
                session_end_raw = leave_dt if leave_dt else None

                ol_start = max(session_start_raw, effective_start)
                ol_end = session_end_raw if session_end_raw else now_utc
                ol_end = min(ol_end, effective_end)

                if ol_end > ol_start:
                    dur = (ol_end - ol_start).total_seconds()
                    online_seconds += dur
                    overlap_sessions += 1
                    if first_online_dt is None:
                        first_online_dt = ol_start
                    last_online_dt = ol_end

                    if session_end_raw and session_end_raw <= effective_end:
                        last_overlap_leave = session_end_raw

                    is_online = session_end_raw is None

            elif ev["action"] in ("leave", "left"):
                pass
            i += 1

        prev_leave_ol = None
        for ev in deduped:
            if ev["action"] in ("leave", "left"):
                lv_dt = datetime.fromisoformat(ev["action_time"])
                if lv_dt.tzinfo is None:
                    lv_dt = lv_dt.replace(tzinfo=timezone.utc)
                if effective_start <= lv_dt <= effective_end:
                    prev_leave_ol = lv_dt
            elif ev["action"] in ("enter", "joined"):
                en_dt = datetime.fromisoformat(ev["action_time"])
                if en_dt.tzinfo is None:
                    en_dt = en_dt.replace(tzinfo=timezone.utc)
                if en_dt >= effective_start and prev_leave_ol:
                    away_raw = (en_dt - prev_leave_ol).total_seconds()
                    if away_raw > 0:
                        away_clamped = min(away_raw, (effective_end - prev_leave_ol).total_seconds())
                        away_seconds += away_clamped
                        if away_clamped > max_away:
                            max_away = away_clamped
                        if away_clamped > 15 * 60:
                            away_over_15_count += 1
                    prev_leave_ol = None

        early_leave_seconds = 0
        if overlap_sessions > 0 and not is_online and last_overlap_leave and last_overlap_leave < effective_end:
            early_leave_seconds = int((effective_end - last_overlap_leave).total_seconds())
            if early_leave_seconds < 0:
                early_leave_seconds = 0

        m["shift_online_minutes"] = int(online_seconds) // 60
        m["required_minutes"] = required_seconds // 60
        if required_seconds > 0:
            m["attendance_rate"] = round(online_seconds / required_seconds, 4)
        else:
            m["attendance_rate"] = 1.0
        m["absent_minutes"] = max(0, int((required_seconds - online_seconds) // 60))
        m["away_minutes"] = int(away_seconds) // 60
        m["max_away_minutes"] = int(max_away) // 60
        m["away_over_15_count"] = away_over_15_count
        m["early_leave_minutes"] = early_leave_seconds // 60
        m["first_online"] = first_online_dt.strftime("%H:%M") if first_online_dt else None
        m["last_online"] = last_online_dt.strftime("%H:%M") if last_online_dt else None
        m["last_activity"] = last_online_dt.isoformat() if last_online_dt else None
        m["sessions"] = overlap_sessions
        m["meeting_not_open_minutes"] = meeting_not_open_seconds // 60

        if is_online:
            m["status"] = "online"
        elif overlap_sessions > 0:
            m["status"] = "offline"
        else:
            m["status"] = "absent"

    if member_names:
        result = []
        for name in member_names:
            if name in members:
                result.append(members[name])
            else:
                from db import resolve_display_name as _r
                resolved = _r(name)
                result.append({
                    "standard_name": name,
                    "raw_name": resolved.get("raw_name", name),
                    "group_name": _batch_group_lookup(name),
                    "status": "absent",
                    "shift_online_minutes": 0,
                    "required_minutes": required_seconds // 60,
                    "attendance_rate": 0.0,
                    "absent_minutes": required_seconds // 60 if required_seconds > 0 else 0,
                    "away_minutes": 0,
                    "max_away_minutes": 0,
                    "away_over_15_count": 0,
                    "early_leave_minutes": 0,
                    "first_online": None,
                    "last_online": None,
                    "last_activity": None,
                    "sessions": 0,
                    "meeting_not_open_minutes": meeting_not_open_seconds // 60,
                })
    else:
        result = list(members.values())

    result.sort(key=lambda x: (
        0 if x["status"] == "online" else 1 if x["status"] == "offline" else 2,
        -x["shift_online_minutes"],
    ))

    return result, {
        "meeting_start": meeting_start.isoformat() if meeting_start else None,
        "effective_start": effective_start.isoformat(),
        "effective_end": effective_end.isoformat(),
        "required_minutes": required_seconds // 60,
        "meeting_not_open_minutes": meeting_not_open_seconds // 60,
    }


def get_sharing_day_stats(tenant_id: str) -> dict:
    """获取当前 MYT 日的共享统计信息"""
    conn = _get_conn()
    from datetime import datetime, timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    now_myt = datetime.now(timezone.utc).astimezone(MYT)
    today_start_utc = (now_myt - timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end_utc = today_start_utc + timedelta(days=1)

    sql = """
        SELECT 
            COUNT(*) AS total,
            SUM(CASE WHEN is_active=1 THEN 1 ELSE 0 END) AS online,
            SUM(CASE WHEN end_time IS NOT NULL AND end_time!='' THEN 
                CAST(ROUND((julianday(end_time) - julianday(start_time)) * 86400) AS INTEGER)
            ELSE 0 END) AS total_duration_sec,
            COUNT(DISTINCT user_name) AS active_users
        FROM sharing_live 
        WHERE tenant_id=? AND start_time>=? AND start_time<?
    """
    row = conn.execute(sql, (tenant_id, today_start_utc.isoformat(), today_end_utc.isoformat())).fetchone()
    total = row[0] or 0
    online = row[1] or 0
    total_duration_sec = row[2] or 0
    active_users = row[3] or 0

    return {
        "total": total,
        "online": online,
        "total_duration": _fmt_dur(total_duration_sec),
        "total_duration_sec": total_duration_sec,
        "active_users": active_users,
    }


def get_sharing_trend(tenant_id: str, hours: int = 24) -> list[dict]:
    """获取最近 N 小时每小时共享次数"""
    conn = _get_conn()
    from datetime import datetime, timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    now_myt = datetime.now(timezone.utc).astimezone(MYT)
    threshold_utc = (now_myt - timedelta(hours=hours)).astimezone(timezone.utc)

    rows = conn.execute(
        "SELECT start_time FROM sharing_live WHERE tenant_id=? AND start_time>=?",
        (tenant_id, threshold_utc.isoformat())
    ).fetchall()

    buckets = {}
    for (st,) in rows:
        if not st:
            continue
        try:
            dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
            myt_hour = dt.astimezone(MYT).strftime("%H:00")
            buckets[myt_hour] = buckets.get(myt_hour, 0) + 1
        except:
            pass

    result = []
    for h in range(24):
        label = f"{h:02d}:00"
        result.append({"hour": label, "count": buckets.get(label, 0)})
    return result


def get_sharing_rank(tenant_id: str) -> list[dict]:
    """获取今日共享时长排行（TOP 10）"""
    from datetime import datetime, timezone, timedelta
    MYT = timezone(timedelta(hours=8))
    now_myt = datetime.now(timezone.utc).astimezone(MYT)
    today_start_utc = (now_myt - timedelta(hours=8)).replace(hour=0, minute=0, second=0, microsecond=0)
    today_end_utc = today_start_utc + timedelta(days=1)

    conn = _get_conn()
    rows = conn.execute(
        """SELECT user_name, 
                  SUM(CASE WHEN end_time IS NOT NULL AND end_time!='' THEN 
                      CAST(ROUND((julianday(end_time) - julianday(start_time)) * 86400) AS INTEGER)
                  ELSE 0 END) AS total_sec
           FROM sharing_live 
           WHERE tenant_id=? AND start_time>=? AND start_time<?
           GROUP BY user_name
           ORDER BY total_sec DESC
           LIMIT 10""",
        (tenant_id, today_start_utc.isoformat(), today_end_utc.isoformat())
    ).fetchall()

    result = []
    for name, sec in rows:
        result.append({"user_name": name, "duration": _fmt_dur(sec), "duration_seconds": sec})
    return result


def get_sharing_detail(meeting_id: str, tenant_id: str, user_name: str = "") -> dict:
    """获取某条共享记录的详细数据（含同会议参与人数）"""
    conn = _get_conn()
    if user_name:
        row = conn.execute(
            "SELECT * FROM sharing_live WHERE meeting_id=? AND tenant_id=? AND user_name=? ORDER BY id DESC LIMIT 1",
            (meeting_id, tenant_id, user_name)
        ).fetchone()
    else:
        row = conn.execute(
            "SELECT * FROM sharing_live WHERE meeting_id=? AND tenant_id=? ORDER BY id DESC LIMIT 1",
            (meeting_id, tenant_id)
        ).fetchone()
    if not row:
        return {"ok": False}

    d = dict(row)
    try:
        if d.get("end_time"):
            s = d["start_time"].replace("Z", "+00:00") if d.get("start_time") else ""
            e = d["end_time"].replace("Z", "+00:00")
            from datetime import datetime, timezone
            st = datetime.fromisoformat(s) if s else None
            et = datetime.fromisoformat(e)
            if et.tzinfo is None: et = et.replace(tzinfo=timezone.utc)
            if st and st.tzinfo is None: st = st.replace(tzinfo=timezone.utc)
            dur_sec = int((et - st).total_seconds()) if st else 0
            d["duration"] = _fmt_dur(dur_sec)
        else:
            d["duration"] = "共享中"
    except:
        d["duration"] = "—"

    # 同会议参与人数
    participant_count = conn.execute(
        "SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE meeting_id=? AND tenant_id=?",
        (meeting_id, tenant_id)
    ).fetchone()[0] or 0
    d["participant_count"] = participant_count

    return {"ok": True, "detail": d}


def get_live_online_standard_names(tenant_id: str) -> set:
    """通过 Zoom Metrics API 获取当前在线成员的标准名集合。

   与 /dashboard/participants 的 status==online 使用同一数据源。
   返回 set[str] — resolve_display_name 后的 standard_name。
   """
    from zoom_metrics import ZoomMetrics

    conn = _get_conn()
    accounts = conn.execute(
        "SELECT * FROM zoom_accounts WHERE tenant_id = ? ORDER BY created_at DESC",
        (tenant_id,),
    ).fetchall()
    accounts = [dict(a) for a in accounts]
    active = next(
        (dict(a) for a in accounts if a.get("is_active") and a.get("status") == "active"),
        None,
    )
    if not active:
        return set()

    import asyncio

    zm = ZoomMetrics(active)
    try:
        live_data = asyncio.run(zm.get_live())
    except Exception:
        return set()

    online = set()
    meetings = live_data.get("meetings", [])
    for m in meetings:
        for p in m.get("participants", []):
            name = p.get("name", "").strip()
            if not name:
                continue
            from db import resolve_display_name

            resolved = resolve_display_name(name, tenant_id=tenant_id)
            sn = resolved.get("display_name", name)
            online.add(sn)
    return online


# ── Stale Session 清理 ────────────────────────────────────────────────────


def cleanup_stale_sessions(tenant_id: str | None = None, dry_run: bool = False) -> dict:
    """
    扫描并修复 participant_sessions 中的 stale open sessions。

    Stale 判定：同一 user_key 有 leave_time_utc IS NULL 的 session，
    但该用户之后又有新的 enter。说明旧 session 的 leave 被漏掉了。

    修复方式：
    - 如果同一 user 后面有 closed session → 截断到后一次 enter 时间
    - 如果完全没后续活动 → leave_time 置为 join_time，duration=0

    返回修正记录数（dry_run 模式只报告不修改）。
    """
    conn = _get_conn()
    fixed = 0
    skipped = 0
    details = []

    where_sql = ""
    params = []
    if tenant_id:
        where_sql = "AND tenant_id = ?"
        params.append(tenant_id)

    # 1. 查出所有 open session
    open_rows = conn.execute(
        "SELECT id, user_key, user_name, join_time_utc, tenant_id "
        "FROM participant_sessions "
        "WHERE leave_time_utc IS NULL AND duration_seconds = 0 "
        f"{where_sql} "
        "ORDER BY user_key, join_time_utc",
        params,
    ).fetchall()

    for row in open_rows:
        row = dict(row)
        sid = row["id"]
        uk = row["user_key"]
        join_utc = row["join_time_utc"]
        t_id = row["tenant_id"]

        # 2. 查同一 user 后续是否有 enter
        later_enters = conn.execute(
            "SELECT id, join_time_utc, leave_time_utc, duration_seconds "
            "FROM participant_sessions "
            "WHERE user_key = ? AND tenant_id = ? AND id != ? "
            "AND join_time_utc > ? "
            "ORDER BY join_time_utc ASC LIMIT 1",
            (uk, t_id, sid, join_utc),
        ).fetchall()

        if later_enters:
            le = dict(later_enters[0])
            next_enter_utc = le["join_time_utc"]
            # 用 next enter 时间作为 leave_time
            dur = int(
                (datetime.fromisoformat(next_enter_utc.replace("Z", "+00:00"))
                 - datetime.fromisoformat(join_utc.replace("Z", "+00:00"))).total_seconds()
            )
            dur = max(dur, 0)

            if dry_run:
                details.append(
                    f"[DRY RUN] session {sid} ({uk}): "
                    f"join={join_utc} → truncate to next enter at {next_enter_utc} ({dur}s)"
                )
            else:
                conn.execute(
                    "UPDATE participant_sessions SET leave_time_utc = ?, duration_seconds = ?, "
                    "updated_at = datetime('now') WHERE id = ?",
                    (next_enter_utc, dur, sid),
                )
                details.append(
                    f"session {sid} ({uk}): join={join_utc} → truncated to {next_enter_utc} ({dur}s)"
                )
            fixed += 1
        else:
            # 完全没后续活动 → 标记为零时长 session
            if dry_run:
                details.append(
                    f"[DRY RUN] session {sid} ({uk}): "
                    f"join={join_utc}, no later activity → zero out"
                )
            else:
                conn.execute(
                    "UPDATE participant_sessions SET leave_time_utc = join_time_utc, "
                    "duration_seconds = 0, updated_at = datetime('now') WHERE id = ?",
                    (sid,),
                )
                details.append(
                    f"session {sid} ({uk}): join={join_utc} → zeroed out"
                )
            fixed += 1

    if not dry_run:
        conn.commit()

    return {
        "ok": True,
        "dry_run": dry_run,
        "fixed": fixed,
        "skipped": skipped,
        "details": details,
    }



