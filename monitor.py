"""
monitor.py — Zoom 轮询监控服务
每 settings.monitor_interval 秒拉参会列表，推送进出/陌生人到 TG
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone, timedelta

from config import settings
from db import save_participant, check_new_email, create_alert
from alerts import TelegramNotifier
from zoom_api import ZoomAPI
import templates as tmpl

MYT = timezone(timedelta(hours=8))
_known: set = set()


def in_push_slot(hour: int) -> bool:
    s, e = settings.push_start_hour, settings.push_end_hour
    return s <= hour < e if s < e else (hour >= s or hour < e)


def get_room_label(mid: str) -> str:
    if mid == settings.zoom_pmi_id:
        return "🏠 自习室(PMI)"
    return f"📅 会议({mid[-4:]})"


def is_late(myt_dt: datetime) -> bool:
    """判断 MYT datetime 是否迟到（signin_deadline_hour 为 MYT）"""
    dh = settings.signin_deadline_hour
    return myt_dt.hour > dh or (myt_dt.hour == dh and myt_dt.minute > 0)


async def monitor_loop():
    zoom = ZoomAPI()
    tg = TelegramNotifier()

    sys.stdout.write("[MONITOR] 启动 Zoom 轮询服务\n")
    sys.stdout.flush()

    while True:
        try:
            now_utc = datetime.now(timezone.utc)
            now_myt = now_utc.astimezone(MYT)
            now_hour = now_myt.hour
            push_now = in_push_slot(now_hour)

            # 收集所有会议 ID
            config_ids = settings.all_meeting_ids
            auto_ids = await zoom.get_scheduled_meetings()
            all_ids = list(set(config_ids + auto_ids))

            new_entries: list = []
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

                    utc_dt = ZoomAPI.parse_zoom_utc(utc_str)
                    if not utc_dt:
                        continue
                    if abs((utc_dt - now_utc).total_seconds()) > 1800:
                        continue

                    key = f"enter_{name}|{utc_str}|{mid}"
                    if key in _known:
                        continue
                    _known.add(key)

                    new_entries.append((name, utc_dt, mid, email))
                    save_participant(mid, name, email, "enter", utc_dt, source="poll")

            # 离开检测
            leaves: list = []
            now_ts = now_utc.timestamp()
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

                    utc_dt = ZoomAPI.parse_zoom_utc(leave_time_str)
                    if not utc_dt:
                        continue
                    if abs(utc_dt.timestamp() - now_ts) > 1800:
                        continue

                    key = f"leave_{name}|{leave_time_str}|{mid}"
                    if key in _known:
                        continue
                    _known.add(key)

                    leaves.append((name, utc_dt, mid))
                    save_participant(mid, name, "", "leave", utc_dt, source="poll")

            # 陌生人检测
            stranger_warnings = []
            for name, utc_dt, mid, email in new_entries:
                if check_new_email(email, name, utc_dt):
                    stranger_warnings.append((name, email, utc_dt, mid))
                    create_alert(
                        alert_type="stranger",
                        title=f"陌生来访: {name}",
                        message=f"邮箱 {email} 首次出现",
                        severity="warning",
                        related_name=name,
                        related_email=email,
                    )

            # 推送陌生人
            if stranger_warnings and push_now:
                lines = []
                for name, email, utc_dt, mid in stranger_warnings:
                    room = get_room_label(mid)
                    myt_time = utc_dt.astimezone(MYT).strftime("%H:%M")
                    lines.append(
                        tmpl.render("stranger_alert",
                                    name=name, email=email,
                                    time=myt_time) +
                        f" [{room}]" if name else ""
                    )
                msg = (tmpl.render("stranger_header", count=str(len(stranger_warnings))) +
                       "\n".join(lines))
                await tg.send(msg)

            # 推送新进
            if new_entries and push_now:
                new_entries.sort(key=lambda x: x[1])
                lines = [tmpl.render("participant_enter_header", count=str(len(new_entries)))]
                for name, utc_dt, mid, _ in new_entries:
                    room = get_room_label(mid)
                    myt_time = utc_dt.astimezone(MYT).strftime("%H:%M")
                    late = " ⚠️迟到" if is_late(utc_dt.astimezone(MYT)) else ""
                    lines.append(
                        tmpl.render("participant_enter",
                                    name=name, time=myt_time,
                                    room=room) + late
                    )
                msg = "\n".join(filter(None, lines))
                await tg.send(msg, group=True)

            # 推送离开
            if leaves and push_now:
                lines = [tmpl.render("participant_leave_header", count=str(len(leaves)))]
                for name, utc_dt, mid in leaves:
                    room = get_room_label(mid)
                    myt_time = utc_dt.astimezone(MYT).strftime("%H:%M")
                    lines.append(
                        tmpl.render("participant_leave",
                                    name=name, time=myt_time,
                                    room=room)
                    )
                msg = "\n".join(filter(None, lines))
                await tg.send(msg, group=True)

            # 日志
            parts = []
            if new_entries:
                parts.append(f"入{len(new_entries)}")
            if stranger_warnings:
                parts.append(f"新{len(stranger_warnings)}")
            if leaves:
                parts.append(f"离{len(leaves)}")
            detail = " ".join(parts) if parts else "无新记录"
            detail += f" {'推送' if push_now else '静默'}"
            sys.stdout.write(f"[{now_utc.strftime('%H:%M')}] {detail}\n")
            sys.stdout.flush()

        except Exception as e:
            sys.stdout.write(f"[MONITOR ERROR] {e}\n")
            sys.stdout.flush()

        await asyncio.sleep(settings.monitor_interval)
