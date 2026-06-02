#!/usr/bin/env python3
"""
scripts/migrate_db.py — 旧 tracking.db → 新 SQLite ORM 迁移
保留原 tracking.db，在新位置 data/tracking.db 重建
"""
import sys
import os
from pathlib import Path
from datetime import datetime

# 确保能 import app
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.settings import settings
from app.database import init_db, SyncSession
from app.models import (
    SeenEmail, DailyStat, PersonStat, HourlyActivity,
    MonitoredMeeting, ParticipantEvent,
)
from sqlalchemy import text as sa_text


def migrate():
    old_db_path = Path(__file__).parent.parent / "tracking.db"
    if not old_db_path.exists():
        print("[MIGRATE] 旧 tracking.db 不存在，跳过迁移")
        return

    print(f"[MIGRATE] 从 {old_db_path} 迁移数据...")
    import sqlite3

    old_conn = sqlite3.connect(str(old_db_path))
    old_conn.row_factory = sqlite3.Row

    # 初始化新 DB
    init_db()

    with SyncSession() as session:
        # 1. seen_emails
        try:
            rows = old_conn.execute("SELECT * FROM seen_emails").fetchall()
            for r in rows:
                session.merge(SeenEmail(
                    email=r["email"],
                    tenant_id=settings.default_tenant_id,
                    name=r.get("name", ""),
                    first_seen=r.get("first_seen", datetime.utcnow().isoformat()),
                    last_seen=r.get("last_seen", datetime.utcnow().isoformat()),
                    seen_count=r.get("seen_count", 1),
                ))
            session.commit()
            print(f"  [OK] seen_emails: {len(rows)} 条")
        except Exception as e:
            session.rollback()
            print(f"  [SKIP] seen_emails: {e}")

        # 2. daily_stats
        try:
            rows = old_conn.execute("SELECT * FROM daily_stats").fetchall()
            for r in rows:
                session.merge(DailyStat(
                    date=r["date"],
                    tenant_id=settings.default_tenant_id,
                    total_persons=r.get("total_persons", 0),
                    total_duration_minutes=r.get("total_duration_minutes", 0.0),
                    earliest_entry=r.get("earliest_entry", ""),
                    latest_entry=r.get("latest_entry", ""),
                    unique_emails=r.get("unique_emails", 0),
                ))
            session.commit()
            print(f"  [OK] daily_stats: {len(rows)} 条")
        except Exception as e:
            session.rollback()
            print(f"  [SKIP] daily_stats: {e}")

        # 3. person_stats
        try:
            rows = old_conn.execute("SELECT * FROM person_stats").fetchall()
            for r in rows:
                session.merge(PersonStat(
                    date=r["date"],
                    tenant_id=settings.default_tenant_id,
                    name=r["name"],
                    email=r.get("email", ""),
                    first_entry=r.get("first_entry", ""),
                    last_leave=r.get("last_leave", ""),
                    total_duration_minutes=r.get("total_duration_minutes", 0.0),
                    enter_count=r.get("enter_count", 0),
                ))
            session.commit()
            print(f"  [OK] person_stats: {len(rows)} 条")
        except Exception as e:
            session.rollback()
            print(f"  [SKIP] person_stats: {e}")

        # 4. hourly_activity
        try:
            rows = old_conn.execute("SELECT * FROM hourly_activity").fetchall()
            for r in rows:
                session.merge(HourlyActivity(
                    date=r["date"],
                    tenant_id=settings.default_tenant_id,
                    hour=r["hour"],
                    person_count=r.get("person_count", 0),
                ))
            session.commit()
            print(f"  [OK] hourly_activity: {len(rows)} 条")
        except Exception as e:
            session.rollback()
            print(f"  [SKIP] hourly_activity: {e}")

        # 5. zoom_events → webhook_events
        try:
            rows = old_conn.execute("SELECT * FROM zoom_events").fetchall()
            from app.models import WebhookEvent as NewWebhookEvent
            for r in rows:
                session.add(NewWebhookEvent(
                    tenant_id=settings.default_tenant_id,
                    event_type=r.get("event_type", "migrated"),
                    payload=r.get("payload", "{}"),
                    processed=True,
                    processed_at=datetime.utcnow(),
                ))
            session.commit()
            print(f"  [OK] webhook_events: {len(rows)} 条 (从 zoom_events 迁移)")
        except Exception as e:
            session.rollback()
            print(f"  [SKIP] webhook_events: {e}")

    old_conn.close()
    print("[MIGRATE] 迁移完成")


if __name__ == "__main__":
    migrate()
