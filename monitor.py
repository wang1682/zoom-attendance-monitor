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
from db import (save_participant, check_new_email, create_alert,
                should_send_telegram, get_all_active_zoom_accounts)
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

            new_entries.append((name, utc_dt, mid, email))
            save_participant(mid, name, email, "enter", utc_dt,
                             source="poll", tenant_id=tenant_id)

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
            save_participant(mid, name, "", "leave", utc_dt,
                             source="poll", tenant_id=tenant_id)

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

    return new_entries, leaves, stranger_warnings


async def monitor_loop():
    zoom_default = ZoomAPI()
    tg = TelegramNotifier()

    sys.stdout.write("[MONITOR] 启动 Zoom 多租户轮询服务\n")
    sys.stdout.flush()

    cycle_count = 0
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
                    d_new.extend(a_new)
                    d_leaves.extend(a_leaves)
                    d_strangers.extend(a_strangers)
                except Exception as e:
                    sys.stdout.write(f"[MONITOR] 租户 {acct.get('label','?')}: {e}\n")
                    sys.stdout.flush()

            # ── 推送：陌生人 ──
            if d_strangers and push_now:
                lines = []
                for name, email, utc_dt, mid in d_strangers:
                    room = get_room_label(mid)
                    myt_time = utc_dt.astimezone(MYT).strftime("%H:%M")
                    lines.append(
                        tmpl.render("stranger_alert",
                                    name=name, email=email,
                                    time=myt_time) +
                        f" [{room}]" if name else ""
                    )
                msg = (tmpl.render("stranger_header", count=str(len(d_strangers))) +
                       "\n".join(lines))
                await tg.send(msg)

            # ── 推送：新进 ──
            if d_new and push_now:
                d_new.sort(key=lambda x: x[1])
                lines = [tmpl.render("participant_enter_header", count=str(len(d_new)))]
                for name, utc_dt, mid, _ in d_new:
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

            # ── 推送：离开 ──
            if d_leaves and push_now:
                lines = [tmpl.render("participant_leave_header", count=str(len(d_leaves)))]
                for name, utc_dt, mid in d_leaves:
                    room = get_room_label(mid)
                    myt_time = utc_dt.astimezone(MYT).strftime("%H:%M")
                    lines.append(
                        tmpl.render("participant_leave",
                                    name=name, time=myt_time,
                                    room=room)
                    )
                msg = "\n".join(filter(None, lines))
                await tg.send(msg, group=True)

            # ── 日志 ──
            parts = []
            if d_new:
                parts.append(f"入{len(d_new)}")
            if d_strangers:
                parts.append(f"新{len(d_strangers)}")
            if d_leaves:
                parts.append(f"离{len(d_leaves)}")
            detail = " ".join(parts) if parts else "无新记录"
            detail += f" {'推送' if push_now else '静默'}"
            if accounts:
                detail += f" {len(accounts)+1}账号"

                        # ── Periodic online report (多群独立频率) ──
            _M = __import__("sys").modules["__main__"]
            if not hasattr(_M, "_LAST_ONLINE_REPORT"):
                _M._LAST_ONLINE_REPORT = time.time()
            if not hasattr(_M, "_LAST_ONLINE_REPORT_TJ"):
                _M._LAST_ONLINE_REPORT_TJ = time.time()
            _nr = time.time()
            _yc_due = _nr - _M._LAST_ONLINE_REPORT >= 10800
            _tj_due = _nr - _M._LAST_ONLINE_REPORT_TJ >= settings.telegram_group2_report_interval

            if (_yc_due or _tj_due) and push_now:
                import httpx
                from app import resolve_member
                _v2_participants = []
                try:
                    _v2r = httpx.get("http://zoom-api:8000/api/v3/live", timeout=10)
                    if _v2r.status_code == 200:
                        _v2d = _v2r.json()
                        for _m in _v2d.get("data", {}).get("meetings", []):
                            for _p in _m.get("participants", []):
                                _raw = _p.get("name", "").strip()
                                if not _raw:
                                    continue
                                _rm = resolve_member(_raw)
                                _std = _rm["standard_name"]
                                _grp = _rm.get("group_name") or "未分组"
                                _join = _p.get("join_time", "")
                                try:
                                    _join_dt = datetime.fromisoformat(_join.replace("Z", "+00:00"))
                                    _enter = _join_dt.astimezone(MYT).strftime("%H:%M")
                                except Exception:
                                    _enter = _join[11:16] if len(_join) > 11 else "??:??"
                                _mins = _p.get("online_minutes", 0)
                                _h, _m = _mins // 60, _mins % 60
                                _dur = f"{_h}小时{_m}分" if _h > 0 else f"{_m}分钟"
                                _v2_participants.append((_std, _grp, _enter, _dur))
                except Exception:
                    pass
                _grouped = {}
                for _std, _grp, _ent, _dur in _v2_participants:
                    _grouped.setdefault(_grp, []).append((_std, _ent, _dur))
                _ordered = []
                for _g in ("核销", "推进"):
                    if _g in _grouped:
                        _ordered.append((_g, _grouped.pop(_g)))
                for _g, _members in _grouped.items():
                    _ordered.append((_g, _members))
                _lines = [
                    "🟠 实时在线",
                    "",
                    f"⏰ 更新时间：{now_myt.strftime('%m-%d %H:%M')}",
                    f"👥 在线人数：{len(_v2_participants)}",
                    "",
                ]
                if _v2_participants:
                    _g_emoji = {"核销": "🔵", "推进": "🟡"}
                    _idx = 0
                    for _g, _members in _ordered:
                        _emoji = _g_emoji.get(_g, "⚪")
                        _lines.append(f"{_emoji}【{_g}】")
                        for _std, _ent, _dur in _members:
                            _idx += 1
                            _lines.append(f"{_idx}. **{_std}**")
                            _lines.append(f"  ↳ 加入：{_ent}")
                            _lines.append(f"  ↳ 在线：{_dur}")
                        _lines.append("")
                else:
                    _lines.append("当前无人在线")
                    _lines.append("")
                _lines.append(f"📊 共{len(_v2_participants)}人在线")
                _report_text = "\n".join(_lines)

                if _yc_due and should_send_telegram("periodic_online_report"):
                    import db as _db
                    _conn = _db._get_conn()
                    _rule_row = _conn.execute(
                        "SELECT target_channel_id FROM telegram_alert_rules WHERE event_type='periodic_online_report'"
                    ).fetchone()
                    _yc_chat_id = ""
                    if _rule_row and _rule_row[0]:
                        _ch_row = _conn.execute(
                            "SELECT chat_id FROM telegram_channels WHERE id=?", (_rule_row[0],)
                        ).fetchone()
                        if _ch_row and _ch_row[0]:
                            _yc_chat_id = _ch_row[0]
                    sys.stdout.write(f"[PERIODIC REPORT] YC群 chat_id={_yc_chat_id or 'private'}\n")
                    sys.stdout.flush()
                    await tg.send(_report_text, chat_id=_yc_chat_id)
                    _M._LAST_ONLINE_REPORT = _nr

                if _tj_due and settings.telegram_group2_enabled and settings.telegram_group2_chat_id:
                    sys.stdout.write(f"[PERIODIC REPORT] TJ群 chat_id={settings.telegram_group2_chat_id}\n")
                    sys.stdout.flush()
                    await tg.send(_report_text, chat_id=settings.telegram_group2_chat_id)
                    _M._LAST_ONLINE_REPORT_TJ = _nr

            sys.stdout.write(f"[{now_utc.strftime('%H:%M')}] {detail}\n")
            sys.stdout.flush()

        except Exception as e:
            sys.stdout.write(f"[MONITOR ERROR] {e}\n")
            sys.stdout.flush()

        await asyncio.sleep(settings.monitor_interval)
