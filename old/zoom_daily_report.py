#!/usr/bin/env python3
"""
zoom_daily_report.py — Zoom 每日统计日报
全部配置从 app.settings 读取（.env），不硬编码任何 secret/ID
"""
import os, sys, json, time, requests
from datetime import datetime, timezone, timedelta

# 从新架构读配置
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.settings import settings
from app.database import SyncSession
from app.models import DailyStat, PersonStat

MYT = timezone(timedelta(hours=8))
SENT_FILE = "/tmp/zoom_daily_sent.txt"

_token_cache = {"token": "", "expires_at": 0}


def get_zoom_token():
    now = time.time()
    if _token_cache["token"] and now < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    resp = requests.post(
        "https://zoom.us/oauth/token",
        data={"grant_type": "account_credentials", "account_id": settings.zoom_account_id},
        auth=(settings.zoom_client_id, settings.zoom_client_secret),
        timeout=10
    )
    resp.raise_for_status()
    data = resp.json()
    _token_cache["token"] = data["access_token"]
    _token_cache["expires_at"] = now + data["expires_in"]
    return data["access_token"]


def zoom_get(path, params=None):
    token = get_zoom_token()
    resp = requests.get(
        f"https://api.zoom.us/v2{path}",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
        timeout=15
    )
    if resp.status_code == 204:
        return {}
    resp.raise_for_status()
    return resp.json()


def get_participants_all_pages(mid, from_dt, to_dt):
    all_p = []
    token = ""
    while True:
        params = {"page_size": 300, "from": from_dt, "to": to_dt}
        if token:
            params["next_page_token"] = token
        try:
            result = zoom_get(f"/report/meetings/{mid}/participants", params)
        except Exception as e:
            print(f"[WARN] 拉取会议 {mid} 失败: {e}")
            break
        all_p.extend(result.get("participants", []))
        token = result.get("next_page_token", "")
        if not token:
            break
    return all_p


def utc_to_myt_dt(utc_str):
    try:
        dt = datetime.strptime(utc_str.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
        return dt.astimezone(MYT)
    except:
        return None


def fmt_time(dt):
    return dt.strftime("%H:%M") if dt else "-"


def send_tg(msg):
    if not settings.telegram_bot_token:
        print("[TG] 未配置 Bot Token，跳过推送")
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_private_chat_id, "text": msg, "parse_mode": "HTML"},
            timeout=10
        )
        if resp.status_code == 200:
            print("[TG] 推送成功")
            return True
        else:
            print(f"[TG] 推送失败: {resp.status_code} {resp.text[:200]}")
            return False
    except Exception as e:
        print(f"[TG] 推送异常: {e}")
        return False


def main():
    today_bj = datetime.now(MYT)
    yesterday = today_bj - timedelta(days=1)
    date_str = yesterday.strftime("%Y-%m-%d")
    weekday_cn = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"][yesterday.weekday()]

    # 日级去重
    if os.path.exists(SENT_FILE):
        with open(SENT_FILE) as f:
            sent_date = f.read().strip()
        if sent_date == date_str:
            print(f"[SKIP] {date_str} 日报已发送过")
            return

    print(f"[REPORT] 生成 {date_str} ({weekday_cn}) 日报")

    meeting_ids = [settings.zoom_pmi_id] + settings.zoom_extra_meeting_ids
    stats = {}

    for mid in meeting_ids:
        participants = get_participants_all_pages(mid, date_str, date_str)
        print(f"  会议 {mid}: 获取到 {len(participants)} 条记录")

        user_records = {}
        for p in participants:
            name = p.get("name", "").strip()
            if not name or name in ["Zoom Room", ""]:
                continue
            if p.get("user_email", "") == settings.zoom_host_email:
                continue

            join_dt = utc_to_myt_dt(p.get("join_time", ""))
            leave_dt = utc_to_myt_dt(p.get("leave_time", ""))
            if not join_dt:
                continue

            if name not in user_records:
                user_records[name] = []
            user_records[name].append({"join": join_dt, "leave": leave_dt})

        for name, records in user_records.items():
            records.sort(key=lambda r: r["join"])
            if name not in stats:
                stats[name] = {
                    "count": 0, "total_sec": 0,
                    "first_join": records[0]["join"],
                    "last_leave": records[-1]["leave"] if records[-1]["leave"] else records[0]["join"],
                }
            else:
                if records[0]["join"] < stats[name]["first_join"]:
                    stats[name]["first_join"] = records[0]["join"]
                if records[-1]["leave"] and records[-1]["leave"] > stats[name]["last_leave"]:
                    stats[name]["last_leave"] = records[-1]["leave"]

            for r in records:
                stats[name]["count"] += 1
                if r["join"] and r["leave"]:
                    sec = (r["leave"] - r["join"]).total_seconds()
                    if sec > 0:
                        stats[name]["total_sec"] += sec
                elif r["join"] and not r["leave"]:
                    stats[name]["total_sec"] += 60

    if not stats:
        msg = f"📊 <b>Zoom 自习室日报</b>\n📅 {date_str} ({weekday_cn})\n\n📭 今日无人参会"
        send_tg(msg)
        with open(SENT_FILE, "w") as f:
            f.write(date_str)
        print(f"[DONE] 无人日报已发送")
        return

    sorted_users = sorted(stats.items(), key=lambda x: x[1]["total_sec"], reverse=True)
    total_visits = sum(s["count"] for _, s in sorted_users)

    lines = [f"📊 <b>Zoom 自习室日报</b>\n📅 {date_str} ({weekday_cn})\n"]
    lines.append(f"👥 今日 {len(stats)} 人出席 | 共 {total_visits} 次进出\n")

    for i, (name, s) in enumerate(sorted_users, 1):
        hours = s["total_sec"] // 3600
        mins = (s["total_sec"] % 3600) // 60
        duration_str = f"{hours}h{mins:02d}m" if hours > 0 else f"{mins}m"
        first_t = fmt_time(s["first_join"])
        last_t = fmt_time(s["last_leave"]) if s["last_leave"] else "-"
        badge = ["🥇", "🥈", "🥉"][i-1] if i <= 3 else f"{i}."
        lines.append(f"{badge} {name}")
        lines.append(f"   ⏱ {duration_str} | {s['count']}次 | {first_t}→{last_t}")

    msg = "\n".join(lines)
    send_tg(msg)

    with open(SENT_FILE, "w") as f:
        f.write(date_str)
    print(f"[DONE] {date_str} 日报已发送")


if __name__ == "__main__":
    main()
