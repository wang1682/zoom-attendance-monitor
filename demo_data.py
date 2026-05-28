"""
demo_data.py — Demo / 演示用假数据生成器
无外部依赖，使用时向 tracking.db 注入数据
"""
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = Path(__file__).parent
DB_PATH = os.getenv("ZOOM_DB_PATH", str(BASE / "data" / "tracking.db"))

# 虚拟人物库
NAMES = [
    ("张三", "zhang.san@example.com"),
    ("李四", "li.si@example.com"),
    ("王五", "wang.wu@example.com"),
    ("赵六", "zhao.liu@example.com"),
    ("孙七", "sun.qi@example.com"),
    ("周八", "zhou.ba@example.com"),
    ("吴九", "wu.jiu@example.com"),
    ("郑十", "zheng.shi@example.com"),
    ("陈小明", "chen.xiaoming@example.com"),
    ("林小红", "lin.xiaohong@example.com"),
    ("黄大力", "huang.dali@example.com"),
    ("杨小梅", "yang.xiaomei@example.com"),
    ("刘建国", "liu.jianguo@example.com"),
    ("张丽华", "zhang.lihua@example.com"),
    ("王大锤", "wang.dachui@example.com"),
    ("李小萌", "li.xiaomeng@example.com"),
    ("赵铁柱", "zhao.tiezhu@example.com"),
    ("陈小白", "chen.xiaobai@example.com"),
    ("周星星", "zhou.xingxing@example.com"),
    ("吴大志", "wu.dazhi@example.com"),
    ("测试用户A", "test.a@example.com"),
    ("测试用户B", "test.b@example.com"),
    ("迟到王", "late.king@example.com"),
    ("全勤君", "fulla@example.com"),
]

def _get_conn():
    os.makedirs(str(BASE / "data"), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn

def ensure_demo_tables():
    """确保 demo 相关表存在（仅创建不存在的表，不覆盖现有 schema）"""
    conn = _get_conn()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS zoom_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_type TEXT NOT NULL,
            payload TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS seen_emails (
            email TEXT PRIMARY KEY,
            first_seen TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT
        );
        CREATE TABLE IF NOT EXISTS demo_seed (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            seeded_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
    """)
    conn.commit()
    conn.close()

def seed_demo_data(days: int = 7):
    """注入 demo 数据，如果已经 seed 过则跳过"""
    ensure_demo_tables()
    conn = _get_conn()
    row = conn.execute("SELECT 1 FROM demo_seed LIMIT 1").fetchone()
    if row:
        conn.close()
        return False  # already seeded

    now = datetime.now(timezone.utc)
    meeting_id = "00000000000"  # demo

    # 遍历最近 days 天
    for day_offset in range(days):
        day = now - timedelta(days=day_offset)
        day_start = day.replace(hour=7, minute=0, second=0, microsecond=0)
        day_end = day.replace(hour=23, minute=0, second=0, microsecond=0)
        if day_offset == 0:
            day_start = now - timedelta(hours=2)  # 今天从2小时前开始

        # 每天 8-20 人在线
        n_people = random.randint(8, 20)
        participants = random.sample(NAMES, min(n_people, len(NAMES)))

        for name, email in participants:
            enter_offset = random.randint(0, 60)  # 进入时间偏移（分钟）
            enter_time = day_start + timedelta(minutes=enter_offset)
            stay_minutes = random.randint(30, 180)
            leave_time = enter_time + timedelta(minutes=stay_minutes)

            # 写入入场记录
            conn.execute(
                "INSERT INTO zoom_participants (meeting_id, name, email, action, action_time, source) VALUES (?,?,?,?,?,?)",
                (meeting_id, name, email, "enter", enter_time.strftime("%Y-%m-%d %H:%M:%S"), "demo")
            )
            # 写入离场记录
            conn.execute(
                "INSERT INTO zoom_participants (meeting_id, name, email, action, action_time, source) VALUES (?,?,?,?,?,?)",
                (meeting_id, name, email, "leave", leave_time.strftime("%Y-%m-%d %H:%M:%S"), "demo")
            )

        # 每天产生 1-3 条告警
        n_alerts = random.randint(1, 3)
        for _ in range(n_alerts):
            p = random.choice(participants)
            alert_time = day_start + timedelta(minutes=random.randint(0, 120))
            alert_type = random.choice(["stranger_alert", "signin_reminder", "overtime"])
            messages = {
                "stranger_alert": f"⚠️ 陌生人: {p[0]} ({p[1]})",
                "signin_reminder": f"⏰ 签到提醒: {p[0]} 尚未签到",
                "overtime": f"⏰ 超时: {p[0]} 仍在自习室",
            }
            conn.execute(
                "INSERT INTO alerts (alert_type, title, message, severity, related_name, related_email, created_at) VALUES (?,?,?,?,?,?,?)",
                (alert_type, messages[alert_type], messages[alert_type], "warning", p[0], p[1], alert_time.strftime("%Y-%m-%d %H:%M:%S"))
            )

        # seen_emails
        for name, email in participants:
            conn.execute(
                "INSERT OR IGNORE INTO seen_emails (email, first_seen) VALUES (?,?)",
                (email, day_start.strftime("%Y-%m-%d %H:%M:%S"))
            )

        # zoom_events
        conn.execute(
            "INSERT INTO zoom_events (event_type, payload, created_at) VALUES (?,?,?)",
            ("demo.seed", f'{{"day":"{day_start.date()}","participants":{len(participants)}}}',
             day_start.strftime("%Y-%m-%d %H:%M:%S"))
        )

    # settings
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('push_enabled','true')")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('quiet_mode','false')")
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('demo_seed_version','1.0')")

    conn.execute("INSERT INTO demo_seed (seeded_at) VALUES (datetime('now'))")
    conn.commit()
    conn.close()
    return True  # freshly seeded


def get_demo_stats() -> dict:
    """获取演示用统计数据"""
    conn = _get_conn()
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    participant_count = conn.execute(
        "SELECT COUNT(DISTINCT name) FROM zoom_participants WHERE action_time >= ? AND action='enter'",
        (today,)
    ).fetchone()[0] or random.randint(3, 12)

    checkin_rate = conn.execute(
        "SELECT ROUND(AVG(CASE WHEN action_time <= ? || ' 09:00:00' THEN 1.0 ELSE 0.0 END) * 100) FROM zoom_participants WHERE action_time >= ? AND action='enter'",
        (today, today)
    ).fetchone()[0]
    if checkin_rate is None:
        checkin_rate = random.randint(60, 95)

    alert_count = conn.execute(
        "SELECT COUNT(*) FROM alerts WHERE created_at >= ?",
        (today,)
    ).fetchone()[0] or random.randint(1, 5)

    seen_count = conn.execute("SELECT COUNT(*) FROM seen_emails").fetchone()[0] or len(NAMES)
    new_face_count = conn.execute(
        "SELECT COUNT(DISTINCT email) FROM zoom_participants WHERE action_time >= ? AND action='enter' AND email NOT IN (SELECT email FROM seen_emails)",
        (today,)
    ).fetchone()[0] or 0

    recent = conn.execute(
        "SELECT DISTINCT name, email, action_time FROM zoom_participants WHERE action_time >= ? AND action='enter' ORDER BY action_time DESC LIMIT 20",
        (today,)
    ).fetchall()

    seen_emails = set(r["email"] for r in conn.execute("SELECT email FROM seen_emails").fetchall())

    recent_participants = []
    for r in recent:
        recent_participants.append({
            "name": r["name"],
            "email": r["email"],
            "time": datetime.fromisoformat(r["action_time"]).strftime("%H:%M") if isinstance(r["action_time"], str) else r["action_time"],
            "is_new": r["email"] not in seen_emails
        })

    conn.close()
    return {
        "participant_count": participant_count,
        "new_face_count": new_face_count,
        "checkin_rate": checkin_rate,
        "alert_count": alert_count,
        "seen_count": seen_count,
        "recent_participants": recent_participants,
    }


def get_demo_alerts(limit: int = 20) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, alert_type as event_type, title as message, created_at FROM alerts ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_demo_participants(limit: int = 50) -> list:
    conn = _get_conn()
    rows = conn.execute(
        "SELECT id, name, email, action, action_time, source FROM zoom_participants ORDER BY id DESC LIMIT ?",
        (limit,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def reset_demo():
    """清除 demo 种子标记，下次 seed_demo_data() 重新生成"""
    conn = _get_conn()
    conn.execute("DELETE FROM demo_seed")
    conn.execute("DELETE FROM zoom_participants")
    conn.execute("DELETE FROM zoom_events")
    conn.execute("DELETE FROM alerts")
    conn.execute("DELETE FROM seen_emails")
    conn.commit()
    conn.close()
