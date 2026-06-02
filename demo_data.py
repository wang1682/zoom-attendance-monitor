"""
demo_data.py — Demo 模式 mock 数据生成器

DEMO_MODE=true 时用此模块替代真实 Zoom API 和 Telegram。
所有数据纯 mock，不影响生产 DB。使用隔离的 demo.db。
"""
from __future__ import annotations

import json
import random
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import db

BASE = Path(__file__).parent
DEMO_DB = BASE / "data" / "demo.db"
_local = threading.local()

# ── Mock 数据源 ──────────────────────────────────────────────────────────────

DEMO_MEETINGS = {
    "8033037476": "自习室",
    "83595997615": "小组讨论室",
}

MOCK_NAMES = [
    ("张三", "zhangsan@example.com"),
    ("李四", "lisi@example.com"),
    ("王小明", "wangxm@example.com"),
    ("赵美丽", "zhaoml@example.com"),
    ("陈大伟", "chendw@example.com"),
    ("刘小霞", "liuxx@example.com"),
    ("周建国", "zhoujg@example.com"),
    ("吴婷婷", "wutt@example.com"),
    ("黄志强", "huangzq@example.com"),
    ("林晓晓", "linxx@example.com"),
    ("何文静", "hewj@example.com"),
    ("马致远", "mazy@example.com"),
    ("孙婉婷", "sunwt@example.com"),
    ("朱宇航", "zuyh@example.com"),
    ("胡少斌", "husb@example.com"),
    ("许嘉欣", "xujx@example.com"),
    ("郑浩然", "zhenghr@example.com"),
    ("罗子涵", "luozh@example.com"),
]

MOCK_NEWCOMERS = [
    ("欧阳娜娜", "ouyangnn@example.com"),
    ("慕容小白", "murongxb@example.com"),
    ("上官婉儿", "shangguanwe@example.com"),
]

MOCK_ALERT_TYPES = [
    ("new_face", "新人出现", "warning"),
    ("checkin_reminder", "签到提醒", "info"),
    ("late", "迟到标记", "error"),
    ("overtime", "超时提醒", "warning"),
    ("summary", "阶段汇总", "info"),
    ("system", "系统通知", "info"),
]


def _get_conn() -> sqlite3.Connection:
    """线程级单例连接，使用独立的 demo.db"""
    if not hasattr(_local, "conn") or _local.conn is None:
        DEMO_DB.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(DEMO_DB))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.row_factory = sqlite3.Row
        _local.conn = conn
    return _local.conn


def _init_demo_tables():
    """创建和 production 一致的 demo 表结构"""
    conn = _get_conn()
    sqls = [
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
        "CREATE TABLE IF NOT EXISTS ai_reports ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT,"
        "  report_type TEXT NOT NULL,"
        "  title TEXT NOT NULL,"
        "  content TEXT NOT NULL,"
        "  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP"
        ")",
    ]
    for sql in sqls:
        conn.execute(sql)
    conn.commit()


def _is_seeded() -> bool:
    db = _get_conn()
    try:
        row = db.execute("SELECT COUNT(*) as cnt FROM zoom_participants").fetchone()
        return row["cnt"] > 0
    except sqlite3.OperationalError:
        return False


def seed_demo_data(force: bool = False):
    """写入 demo 数据到独立的 demo.db
    
    只在首次或 force=True 时执行，避免重复写入。
    DEMO_MODE 下读取 demo.db，不影响 production tracking.db。
    """
    _init_demo_tables()
    if _is_seeded() and not force:
        return

    conn = _get_conn()
    now = datetime.now(timezone.utc)
    today = now.strftime("%Y-%m-%d")

    # ── Participants ──
    # 生成今天早到/准时/迟到/未到等典型场景
    random.seed(42)  # 固定种子使 demo 数据可复现

    # 进出记录
    for i, (name, email) in enumerate(MOCK_NAMES):
        meeting = random.choice(list(DEMO_MEETINGS.keys()))
        join_offset = random.randint(0, 360)  # 0~360 分钟内抵达
        join_time = now.replace(hour=8, minute=0, second=0) + timedelta(minutes=join_offset)
        leave_offset = random.randint(30, 180)
        leave_time = join_time + timedelta(minutes=leave_offset)

        conn.execute(
            "INSERT INTO zoom_participants (meeting_id, name, email, action, action_time, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (meeting, name, email, "enter", join_time.isoformat(), "poll"),
        )
        # 部分人有 leave 记录
        if random.random() < 0.6:
            conn.execute(
                "INSERT INTO zoom_participants (meeting_id, name, email, action, action_time, source) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (meeting, name, email, "leave", leave_time.isoformat(), "poll"),
            )
        # seen_emails
        conn.execute(
            "INSERT OR IGNORE INTO seen_emails (email, name, first_seen, last_seen, seen_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, name, join_time.isoformat(), join_time.isoformat(), random.randint(1, 20)),
        )

    # 新人 (今天第一次出现)
    for name, email in MOCK_NEWCOMERS:
        meeting = list(DEMO_MEETINGS.keys())[0]
        join_time = now.replace(hour=8, minute=0, second=0) + timedelta(minutes=random.randint(10, 60))
        conn.execute(
            "INSERT INTO zoom_participants (meeting_id, name, email, action, action_time, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (meeting, name, email, "enter", join_time.isoformat(), "webhook"),
        )
        conn.execute(
            "INSERT INTO seen_emails (email, name, first_seen, last_seen, seen_count) "
            "VALUES (?, ?, ?, ?, ?)",
            (email, name, join_time.isoformat(), join_time.isoformat(), 1),
        )

    # ── Events ──
    event_types = ["meeting.participant_joined", "meeting.participant_left",
                   "meeting.started", "meeting.ended"]
    for i in range(30):
        etype = random.choice(event_types)
        payload = json.dumps({
            "event": etype,
            "payload": {"object": {"id": random.choice(list(DEMO_MEETINGS.keys())), "topic": "Demo Meeting"}}
        })
        event_time = (now - timedelta(minutes=random.randint(0, 600))).isoformat()
        conn.execute(
            "INSERT INTO zoom_events (event_type, payload, created_at) VALUES (?, ?, ?)",
            (etype, payload, event_time),
        )

    # ── Alerts ──
    for i in range(15):
        atype, title, severity = random.choice(MOCK_ALERT_TYPES)
        name, email = random.choice(MOCK_NAMES + MOCK_NEWCOMERS)
        alert_time = (now - timedelta(minutes=random.randint(0, 500))).isoformat()
        conn.execute(
            "INSERT INTO alerts (alert_type, severity, title, message, related_name, "
            "  related_email, sent_to, success, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (atype, severity, title,
             f"{name} 参加了会议" if atype == "new_face"
             else f"在 {random.randint(7,9)}:00 前未签到" if atype == "late"
             else f"参会时长超过 {random.randint(60,180)} 分钟" if atype == "overtime"
             else f"阶段汇总: {random.randint(5,20)} 人在线",
             name, email,
             "telegram:7922047310",
             1 if random.random() < 0.8 else 0,
             alert_time),
        )

    # ── AI Reports ──
    reports = [
        ("daily", "今日出勤报告",
         "## 出勤概要\n\n"
         "- 应到: 18 人 | 实到: 15 人 | 出勤率: **83%**\n"
         "- 迟到: 2 人 (11%) | 缺勤: 3 人 (17%)\n"
         "- 新人: 3 人 (欧阳娜娜, 慕容小白, 上官婉儿)\n\n"
         "### 趋势分析\n"
         "本周出勤率稳定在 80-85%，周环比 +3%。\n"
         "建议关注迟到率偏高的 9:00-9:15 时段。"),
        ("weekly", "本周出勤分析报告",
         "## 本周总览\n\n"
         "| 指标 | 数值 | 环比 |\n"
         "|------|------|------|\n"
         "| 总参会人次 | 132 | +7% |\n"
         "| 平均时长 | 95 分钟 | +12 分钟 |\n"
         "| 迟到率 | 14% | -2% |\n"
         "| 新人占比 | 8% | +3% |\n\n"
         "### 推荐行动\n"
         "1. 迟到率下降趋势良好，继续保持\n"
         "2. 新人占比上升，建议安排一对一指导\n"),
        ("analytics", "参会行为分析",
         "## 参会行为洞察\n\n"
         "### 高峰时段\n"
         "- **早高峰**: 08:30-08:45（62% 参会者在此时段进入）\n"
         "- **晚高峰**: 17:30-18:00（集中离开）\n\n"
         "### 参与度评分\n"
         "- 高度参与 (>120分钟): 6 人 (33%)\n"
         "- 中等参与 (60-120分钟): 7 人 (39%)\n"
         "- 低参与 (<60分钟): 5 人 (28%)"),
    ]
    for rtype, title, content in reports:
        conn.execute(
            "INSERT INTO ai_reports (report_type, title, content, created_at) VALUES (?, ?, ?, ?)",
            (rtype, title, content, now.isoformat()),
        )

    conn.commit()


def reset_demo():
    """清空 demo.db"""
    DEMO_DB.unlink(missing_ok=True)
    _init_demo_tables()


# ── 查询接口 ─────────────────────────────────────────────────────────────────


def get_demo_stats() -> dict:
    """返回 demo dashboard 统计数据"""
    conn = _get_conn()
    total = conn.execute("SELECT COUNT(DISTINCT name) as c FROM zoom_participants "
                         "WHERE action='enter'").fetchone()["c"]
    now = datetime.now(timezone.utc)
    new_count = conn.execute("SELECT COUNT(*) as c FROM seen_emails "
                             "WHERE first_seen >= ?",
                             (now.replace(hour=0, minute=0, second=0).isoformat(),)
                             ).fetchone()["c"]
    # Demo 模式：使用随机但稳定的签到率（mock 不追求精确边界计算）
    import hashlib
    stable_seed = int(hashlib.md5(str(total).encode()).hexdigest()[:8], 16)
    checkin_rate = round(60 + (stable_seed % 30), 1)  # 60-89%
    alert_count = conn.execute("SELECT COUNT(*) as c FROM alerts").fetchone()["c"]
    return {
        "participant_count": total,
        "new_face_count": new_count,
        "checkin_rate": checkin_rate,
        "alert_count": alert_count,
        "report_count": conn.execute("SELECT COUNT(*) as c FROM ai_reports").fetchone()["c"],
        "meeting_rooms": list(DEMO_MEETINGS.values()),
    }


def get_demo_participants(limit: int = 100) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM zoom_participants ORDER BY action_time DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_demo_alerts(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_demo_events(limit: int = 50) -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM zoom_events ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [dict(r) for r in rows]


def get_demo_reports() -> list[dict]:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT * FROM ai_reports ORDER BY id DESC"
    ).fetchall()
    return [dict(r) for r in rows]


def get_demo_analytics() -> dict:
    """返回 demo analytics 统计"""
    conn = _get_conn()
    rows = conn.execute(
        "SELECT action_time, action, name FROM zoom_participants ORDER BY action_time"
    ).fetchall()
    entries = [r for r in rows if r["action"] == "enter"]

    # 按小时分布
    hourly = {}
    for r in entries:
        try:
            dt = datetime.fromisoformat(r["action_time"])
            h = dt.hour
            hourly[h] = hourly.get(h, 0) + 1
        except (ValueError, TypeError):
            pass

    # 在线人数趋势
    timeline = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["action_time"])
            t = dt.strftime("%H:%M")
            if t not in timeline:
                timeline[t] = {"enter": 0, "leave": 0}
            timeline[t][r["action"]] += 1
        except (ValueError, TypeError):
            pass

    # 汇总在线
    online = 0
    online_timeline = []
    for t in sorted(timeline.keys()):
        online += timeline[t].get("enter", 0)
        online -= timeline[t].get("leave", 0)
        online_timeline.append({"time": t, "online": max(0, online)})

    return {
        "total_participants": len(set(r["name"] for r in rows if r["action"] == "enter")),
        "total_unique": len(set(r["name"] for r in rows)),
        "hourly_distribution": [{"hour": h, "count": c} for h, c in sorted(hourly.items())],
        "online_timeline": online_timeline,
        "avg_duration": 95,
        "peak_hour": max(hourly, key=hourly.get) if hourly else 8,
        "peak_count": max(hourly.values()) if hourly else 0,
    }
