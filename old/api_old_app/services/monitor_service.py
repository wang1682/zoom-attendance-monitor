"""
monitor_service.py — Zoom 轮询监控服务
核心循环：每30s拉参会列表，检测进出/陌生/超时，推送到 Telegram
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone, timedelta

from app.settings import settings
from app.services.event_service import save_participant_event, check_new_email, create_alert
from app.integrations.zoom.api import ZoomAPI
from app.integrations.telegram import TelegramNotifier
from phase2.event_writer import write_participant, write_zoom_event, write_alert_log

MYT = timezone(timedelta(hours=8))

# 内存状态
_known: set = set()
_day_records: dict = {}
_daily_sent: set = set()
_overtime_sent: set = set()


def in_push_slot(hour: int) -> bool:
    start, end = settings.push_start_hour, settings.push_end_hour
    if start < end:
        return start <= hour < end
    else:
        return hour >= start or hour < end


def get_room_label(mid: str) -> str:
    if mid == settings.zoom_pmi_id:
        return "🏠 自习室(PMI)"
    return f"📅 会议({mid[-4:]})"


def is_late(dt: datetime) -> bool:
    dh = settings.signin_deadline_hour
    return dt.hour > dh or (dt.hour == dh and dt.minute > 0)


async def monitor_loop():
    """主轮询循环"""
    zoom = ZoomAPI()
    tg = TelegramNotifier()

    last_summary_hour: int | None = None
    last_daily_hour: int | None = None
    last_trend_hour: int | None = None

    sys.stdout.write("[MONITOR] 启动 Zoom 轮询服务\n")
    sys.stdout.flush()

    while True:
        try:
            now = datetime.now(MYT)
            now_hour = now.hour
            push_now = in_push_slot(now_hour)

            # 获取所有会议 ID
            config_ids = settings.all_meeting_ids
            auto_ids = await zoom.get_scheduled_meetings()
            all_ids = list(set(config_ids + auto_ids))

            new_entries = []

            for mid in all_ids:
                try:
                    participants = await zoom.get_participants(mid)
                except Exception:
                    continue

                for p in participants:
                    name = p.get("name", "").strip()
                    utc_str = p.get("join_time", "")
                    email = p.get("user_email", "")
                    status = p.get("status", "")

                    if email == settings.zoom_host_email or status == "in_waiting_room":
                        continue
                    if not name or name in ("Zoom Room", ""):
                        continue

                    myt_dt = ZoomAPI.utc_to_myt(utc_str)
                    if not myt_dt:
                        continue

                    diff_sec = (myt_dt - now).total_seconds()
                    if diff_sec < -1800 or diff_sec > 300:
                        continue

                    key = f"enter_{name}|{utc_str}|{mid}"
                    if key in _known:
                        continue
                    _known.add(key)

                    new_entries.append((name, myt_dt, mid, email))
                    save_participant_event(mid, name, email, "enter", myt_dt, source="poll",
                                          tenant_id=settings.default_tenant_id)
                    write_participant(mid, name, email, "enter", myt_dt, source="poll",
                                     tenant_id=settings.default_tenant_id)

            # 离开检测
            leaves = []
            now_ts = now.timestamp()

            for mid in all_ids:
                try:
                    participants = await zoom.get_participants(mid)
                except Exception:
                    continue

                for p in participants:
                    name = p.get("name", "").strip()
                    if not name or name in ("Zoom Room", ""):
                        continue
                    email = p.get("user_email", "")
                    if email == settings.zoom_host_email:
                        continue

                    leave_time_str = p.get("leave_time", "")
                    if not leave_time_str:
                        continue
                    if p.get("status") == "in_meeting":
                        continue

                    myt_dt = ZoomAPI.utc_to_myt(leave_time_str)
                    if not myt_dt:
                        continue
                    if abs(myt_dt.timestamp() - now_ts) > 1800:
                        continue

                    key = f"leave_{name}|{leave_time_str}|{mid}"
                    if key in _known:
                        continue
                    _known.add(key)

                    leaves.append((name, myt_dt, mid))
                    save_participant_event(mid, name, "", "leave", myt_dt, source="poll",
                                          tenant_id=settings.default_tenant_id)
                    write_participant(mid, name, "", "leave", myt_dt, source="poll",
                                     tenant_id=settings.default_tenant_id)

            # 陌生人预警
            stranger_warnings = []
            for name, myt_dt, mid, email in new_entries:
                is_new = check_new_email(name, email, myt_dt,
                                          tenant_id=settings.default_tenant_id)
                if is_new:
                    stranger_warnings.append((name, email, myt_dt, mid))
                    create_alert(
                        alert_type="stranger",
                        title=f"陌生来访: {name}",
                        message=f"邮箱 {email} 首次出现",
                        severity="warning",
                        related_name=name,
                        related_email=email,
                        tenant_id=settings.default_tenant_id,
                    )

            # 推送
            if stranger_warnings and push_now:
                lines = [f"👤 **陌生来访预警**", f"发现 {len(stranger_warnings)} 个新面孔", ""]
                for name, email, myt_dt, mid in stranger_warnings:
                    lines.append(f"🔰 {name} ({email})  {myt_dt.strftime('%H:%M')} [{get_room_label(mid)}]")
                await tg.send("\n".join(lines))

            if new_entries and push_now:
                new_entries.sort(key=lambda x: x[1])
                lines = [f"📋 参会汇总 {now.strftime('%H:%M')}", f"新增 {len(new_entries)} 人", ""]
                for name, myt_dt, mid, _ in new_entries:
                    room = get_room_label(mid)
                    late_flag = " ⚠️迟到" if is_late(myt_dt) else ""
                    lines.append(f"👤 {name} {myt_dt.strftime('%H:%M')}{late_flag} [{room}]")
                await tg.send("\n".join(lines), group=True)

            if leaves and push_now:
                lines = ["🚪 **离开提醒**", f"离开 {len(leaves)} 人", ""]
                for name, myt_dt, mid in leaves:
                    lines.append(f"🚶 {name}  {myt_dt.strftime('%H:%M')} [{get_room_label(mid)}]")
                await tg.send("\n".join(lines), group=True)

            # 日志
            detail_parts = []
            if new_entries:
                detail_parts.append(f"入{len(new_entries)}")
            if stranger_warnings:
                detail_parts.append(f"新{len(stranger_warnings)}")
            if leaves:
                detail_parts.append(f"离{len(leaves)}")
            detail = " ".join(detail_parts) if detail_parts else "无新记录"
            detail += f" {'推送' if push_now else '静默'}"
            sys.stdout.write(f"[{now.strftime('%H:%M')}] {detail}\n")
            sys.stdout.flush()

        except Exception as e:
            sys.stdout.write(f"[MONITOR ERROR] {e}\n")
            sys.stdout.flush()

        await asyncio.sleep(settings.monitor_interval)
