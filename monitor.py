"""
monitor.py — Zoom 多租户轮询监控服务
每 settings.monitor_interval 秒遍历所有激活的 Zoom 账号，
分别拉参会列表，推送进出/陌生人到 TG
"""
from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime, timezone, timedelta

from config import settings
from db import (should_send_telegram, get_all_active_zoom_accounts)
from alerts import TelegramNotifier
from zoom_metrics import ZoomMetrics
from zoom_api import ZoomAPI
import templates as tmpl

MYT = timezone(timedelta(hours=8))
_known: set = set()
_report_sent_hours: set = set()  # 已推送过在线报告的 MYT 小时


def in_push_slot(hour: int) -> bool:
    s, e = settings.push_start_hour, settings.push_end_hour
    return s <= hour < e if s < e else (hour >= s or hour < e)

def in_report_slot(hour: int) -> bool:
    # 每 3 小时推一次在线报告（MYT 0, 3, 6, 9, 12, 15, 18, 21）
    return hour % 3 == 0


def get_room_label(mid: str) -> str:
    if mid == settings.zoom_pmi_id:
        return "🏠 自习室(PMI)"
    return f"📅 会议({mid[-4:]})"


def is_late(myt_dt: datetime) -> bool:
    """判断 MYT datetime 是否迟到（signin_deadline_hour 为 MYT）"""
    dh = settings.signin_deadline_hour
    return myt_dt.hour > dh or (myt_dt.hour == dh and myt_dt.minute > 0)


async def poll_account(zoom: ZoomAPI, meeting_ids: list[str],
                       push_now: bool, now_utc: datetime,
                       now_myt: datetime, tg: TelegramNotifier
                       ) -> tuple[list, list, list]:
    """对一个 Zoom 账号执行一次轮询"""
    tenant_id = zoom.tenant_id
    all_ids = meeting_ids[:]

    # 自动检测该账号的已排程会议
    try:
        auto_ids = await zoom.get_scheduled_meetings()
        for aid in auto_ids:
            if aid not in all_ids:
                all_ids.append(aid)
    except Exception:
        pass

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

            key = f"enter_{name}|{utc_str}|{mid}|{tenant_id}"
            if key in _known:
                continue
            _known.add(key)

            from services.participant import ParticipantService

            new_entries.append((name, utc_dt, mid, email))
            ParticipantService.save_participant(mid, name, email, "enter", utc_dt,
                                                tenant_id=tenant_id, source="poll")

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

            key = f"leave_{name}|{leave_time_str}|{mid}|{tenant_id}"
            if key in _known:
                continue
            _known.add(key)

            leaves.append((name, utc_dt, mid))
            ParticipantService.save_participant(mid, name, "", "leave", utc_dt,
                                                tenant_id=tenant_id, source="poll")

    # 陌生人检测
    stranger_warnings = []
    for name, utc_dt, mid, email in new_entries:
        from services.participant import ParticipantService

        if ParticipantService.check_new_participant(email, name, utc_dt):
            stranger_warnings.append((name, email, utc_dt, mid))
            ParticipantService.create_stranger_alert(name, email, utc_dt, mid)

    return new_entries, leaves, stranger_warnings


def _push_by_rule(event_type: str, text: str, tenant_id: str,
                  default_tg: TelegramNotifier) -> asyncio.Task | None:
    """按告警规则推送 — 统一走 telegram_alert_rules + telegram_channels"""
    from db import _get_conn
    conn = _get_conn()
    rule = conn.execute(
        "SELECT id, enabled, target_channel_id FROM telegram_alert_rules "
        "WHERE event_type=? AND enabled=1",
        (event_type,)
    ).fetchone()
    if not rule or not rule["target_channel_id"]:
        return None
    ch = conn.execute(
        "SELECT chat_id, bot_token FROM telegram_channels WHERE id=? AND enabled=1",
        (rule["target_channel_id"],)
    ).fetchone()
    if not ch or not ch["bot_token"]:
        return None
    _tg = TelegramNotifier(token=ch["bot_token"])
    return asyncio.ensure_future(_tg.send(text, chat_id=ch["chat_id"]))


async def _push_entries(entries: list[tuple], entry_type: str, tenant_id: str,
                       push_now: bool, now_myt: datetime, default_tg: TelegramNotifier):
    """Push entry/leave/stranger events for a single tenant."""
    if not entries or not push_now:
        return
    if entry_type == "stranger":
        for name, email, utc_dt, mid in entries:
            room = get_room_label(mid)
            myt_time = utc_dt.astimezone(MYT).strftime("%H:%M")
            msg = tmpl.render("stranger_alert",
                              name=name, email=email,
                              time=myt_time, room=room)
            _fut = _push_by_rule("unknown_user", msg, tenant_id, default_tg)
            if not _fut:
                return

    elif entry_type == "enter":
        entries.sort(key=lambda x: x[1])
        for name, utc_dt, mid, _ in entries:
            room = get_room_label(mid)
            myt_time = utc_dt.astimezone(MYT).strftime("%H:%M")
            late = " ⚠️迟到" if is_late(utc_dt.astimezone(MYT)) else ""
            msg = tmpl.render("participant_enter",
                              name=name, time=myt_time,
                              room=room) + late
            _fut = _push_by_rule("participant_joined", msg, tenant_id, default_tg)
            if not _fut:
                return

    elif entry_type == "leave":
        for name, utc_dt, mid in entries:
            room = get_room_label(mid)
            myt_time = utc_dt.astimezone(MYT).strftime("%H:%M")
            msg = tmpl.render("participant_leave",
                              name=name, time=myt_time,
                              room=room)
            _fut = _push_by_rule("participant_left", msg, tenant_id, default_tg)
            if not _fut:
                return


# ── Official 报表同步 ──

_OFFICIAL_SYNC_CYCLE = 0       # 当前循环计数
_OFFICIAL_SYNC_INTERVAL = 12   # 每 12 个 tick 触发一次（≈ 60 分钟）

async def _run_official_sync(tenant_id: str, zoom_acct: dict | None = None) -> dict:
    """同步 tenant 的昨天+今天官方报表数据到 official_attendance_sessions。
    返回 {tenant, inserted, skipped, errors, date_str, meetings}。

    无 zoom_acct 时尝试从 zoom_accounts 表自动获取。
    """
    import db as _db
    za = zoom_acct or _db.get_zoom_account(tenant_id)
    if not za:
        return {"tenant": tenant_id, "error": "no zoom account"}
    zm = ZoomMetrics(za)
    from datetime import datetime, timezone, timedelta as _td
    now = datetime.now(timezone.utc)
    yesterday_start = (now - _td(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + _td(days=1)
    try:
        meetings = await zm.get_past_meetings(page_size=50, from_days=2)
    except Exception as e:
        return {"tenant": tenant_id, "error": f"get_past_meetings: {e}"}
    total_i = total_s = total_e = 0
    meeting_count = 0
    seen_mids = set()
    for m in meetings:
        mid = str(m.get("id", ""))
        if mid and mid in seen_mids:
            continue
        seen_mids.add(mid)
        # 只处理昨天和今天开始的会议
        st = m.get("start_time", "")
        if st:
            try:
                st_dt = datetime.fromisoformat(st.replace("Z", "+00:00"))
                if st_dt < yesterday_start or st_dt >= today_end:
                    continue
            except Exception:
                pass
        try:
            participants = await zm.get_report_meeting_participants(mid)
        except Exception:
            total_e += 1
            continue
        for p in participants:
            dur = p.get("duration", 0) or 0
            dur_min = dur // 60
            try:
                rid = _db.upsert_official_attendance_session(
                    tenant_id=tenant_id,
                    meeting_id=mid,
                    topic=m.get("topic", mid),
                    host_name=m.get("host_name", ""),
                    host_email=m.get("host_email", ""),
                    meeting_start=st,
                    meeting_end=m.get("end_time", ""),
                    participant_name=p["name"],
                    email=p.get("email", ""),
                    join_time=p.get("join_time", ""),
                    leave_time=p.get("leave_time", ""),
                    duration_minutes=float(dur_min),
                )
                if rid:
                    total_i += 1
                else:
                    total_s += 1
            except Exception:
                total_e += 1
        meeting_count += 1
    return {
        "tenant": tenant_id,
        "inserted": total_i,
        "skipped": total_s,
        "errors": total_e,
        "date_str": today_start.strftime("%Y-%m-%d"),
        "meetings": meeting_count,
    }


async def monitor_loop():
    zoom_default = ZoomAPI()
    tg = TelegramNotifier()

    # 全局循环计数器
    global _OFFICIAL_SYNC_CYCLE
    _OFFICIAL_SYNC_CYCLE = 0

    sys.stdout.write("[MONITOR] 启动 Zoom 多租户轮询服务\n")
    sys.stdout.flush()

    cycle_count = 0
    _known_tenants: set[str] = set()
    while True:
        cycle_count += 1
        try:
            now_utc = datetime.now(timezone.utc)
            now_myt = now_utc.astimezone(MYT)
            now_hour = now_myt.hour
            push_now = in_push_slot(now_hour)
            sys.stdout.write(f"[MONITOR] 轮询#{cycle_count} 开始 @ {now_utc.strftime('%H:%M:%S')}\n")
            sys.stdout.flush()

            # ── 1. 默认账号（.env 配置，保持向后兼容）──
            config_ids = settings.all_meeting_ids
            auto_ids = await zoom_default.get_scheduled_meetings()
            default_ids = list(set(config_ids + auto_ids))

            d_new, d_leaves, d_strangers = await poll_account(
                zoom_default, default_ids, push_now, now_utc, now_myt, tg
            )

            # 默认账号推送到 tenant_id=default 的 channels
            _known_tenants.add("default")
            await _push_entries(d_strangers, "stranger", "default", push_now, now_myt, tg)
            await _push_entries(d_new, "enter", "default", push_now, now_myt, tg)
            await _push_entries(d_leaves, "leave", "default", push_now, now_myt, tg)

            # ── 2. 数据库中所有激活的 Zoom 账号（多租户）──
            accounts = get_all_active_zoom_accounts()
            for acct in accounts:
                try:
                    zoom_acct = ZoomAPI(
                        account_id=acct["account_id"],
                        client_id=acct["client_id"],
                        client_secret=acct["client_secret"],
                        tenant_id=acct["tenant_id"],
                    )
                    zoom_acct.label = acct.get("label", "")

                    # 从 db 获取该账号配置的会议 ID
                    import db as _db
                    meetings = _db.get_monitored_meetings_for_account(acct["id"])
                    acct_ids = [m["meeting_id"] for m in meetings]

                    a_new, a_leaves, a_strangers = await poll_account(
                        zoom_acct, acct_ids, push_now, now_utc, now_myt, tg
                    )

                    # 每个租户各自推送
                    tid = acct["tenant_id"]
                    _known_tenants.add(tid)
                    await _push_entries(a_strangers, "stranger", tid, push_now, now_myt, tg)
                    await _push_entries(a_new, "enter", tid, push_now, now_myt, tg)
                    await _push_entries(a_leaves, "leave", tid, push_now, now_myt, tg)

                    # 合并日志计数
                    d_new.extend(a_new)
                    d_leaves.extend(a_leaves)
                    d_strangers.extend(a_strangers)
                except Exception as e:
                    sys.stdout.write(f"[MONITOR] 租户 {acct.get('label','?')}: {e}\n")
                    sys.stdout.flush()

            # ── 日志 ──
            parts = []
            if d_new:
                parts.append(f"入{len(d_new)}")
            if d_strangers:
                parts.append(f"新{len(d_strangers)}")
            if d_leaves:
                parts.append(f"离{len(d_leaves)}")
            # 常规日志：无变化时每小时打一次，有变化每次都打
            _now_hr = now_utc.hour
            if not parts:
                if getattr(_run_official_sync, '_last_noop_hr', -1) != _now_hr:
                    detail = "无新记录"
                    _run_official_sync._last_noop_hr = _now_hr
                else:
                    detail = None
            else:
                detail = " ".join(parts)

            if detail is not None:
                detail += f" {'推送' if push_now else '静默'}"
                if accounts:
                    detail += f" {len(accounts)+1}账号"

                        # ── Periodic online report (每 3 小时一次，走 ReportService) ──
            if now_hour not in _report_sent_hours:
                _report_sent_hours.add(now_hour)
                from services.report import ReportService
                rs = ReportService()
                for _rt in list(_known_tenants):
                    try:
                        await rs.send_report(_rt)
                    except Exception as _re:
                        sys.stderr.write(f"[PERIODIC REPORT] error for {_rt}: {_re}\n")

            # ── Long online alert (每个 tick 检查，走 LongOnlineAlertService) ──
            from services.long_online_alert import LongOnlineAlertService
            for _rt in list(_known_tenants):
                try:
                    await LongOnlineAlertService.check(_rt)
                except Exception as _le:
                    sys.stderr.write(f"[LONG_ONLINE] error for {_rt}: {_le}\n")

            # ── Official 报表同步 (每 12 tick ≈ 60 分钟) ──
            _OFFICIAL_SYNC_CYCLE += 1
            if _OFFICIAL_SYNC_CYCLE % _OFFICIAL_SYNC_INTERVAL == 0:
                for _rt in list(_known_tenants):
                    try:
                        _za = None
                        if _rt != "default":
                            import db as _db
                            _za = _db.get_zoom_account(_rt)
                        _result = await _run_official_sync(_rt, _za)
                        if _result.get("error"):
                            sys.stderr.write(f"[OFFICIAL_SYNC] tenant={_rt} error={_result['error']}\n")
                        else:
                            sys.stdout.write(
                                f"[OFFICIAL_SYNC] tenant={_rt} date={_result['date_str']} "
                                f"inserted={_result['inserted']} skipped={_result['skipped']} "
                                f"meetings={_result['meetings']}\n"
                            )
                        sys.stdout.flush()
                    except Exception as _oe:
                        sys.stderr.write(f"[OFFICIAL_SYNC] tenant={_rt}: {_oe}\n")

            if detail:
                sys.stdout.write(f"[{now_utc.strftime('%H:%M')}] {detail}\n")
                sys.stdout.flush()

        except Exception as e:
            sys.stdout.write(f"[MONITOR ERROR] {e}\n")
            sys.stdout.flush()

        await asyncio.sleep(settings.monitor_interval)
