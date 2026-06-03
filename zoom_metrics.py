"""
zoom_metrics.py — Zoom Business Metrics API 服务层
所有姓名输出统一经 resolve_display_name()
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timezone

import httpx
from config import settings
import db as _db

_cache = {"live": {"data": None, "ts": 0}}
CACHE_TTL = 30


class ZoomMetrics:
    def __init__(self):
        self._token = ""
        self._expires_at = 0.0

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://zoom.us/oauth/token",
                data={"grant_type": "account_credentials",
                      "account_id": settings.zoom_account_id},
                auth=(settings.zoom_client_id, settings.zoom_client_secret),
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["access_token"]
            self._expires_at = now + data["expires_in"]
            return self._token

    async def _get(self, path: str, params: dict = None) -> dict:
        token = await self._get_token()
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.get(
                f"https://api.zoom.us/v2{path}",
                headers={"Authorization": f"Bearer {token}"},
                params=params or {},
            )
            if r.status_code == 204:
                return {}
            r.raise_for_status()
            return r.json()

    async def get_live(self) -> dict:
        """获取在线数据，所有姓名经 resolve_display_name()"""
        now = time.time()
        cached = _cache["live"]
        if cached["data"] and now - cached["ts"] < CACHE_TTL:
            return cached["data"]

        meetings_data = await self._get(
            "/metrics/meetings", {"type": "live", "page_size": 100}
        )
        meetings_list = meetings_data.get("meetings", [])
        now_utc = datetime.now(timezone.utc)

        result = {"meetings": [], "total_online": 0}
        all_canonical = set()
        enabled_count = 0

        for m in meetings_list:
            mid = str(m.get("id", ""))
            topic = m.get("topic", mid)
            raw_participants = await self._get_participants(mid)

            seen = {}
            for p in raw_participants:
                if p.get("status") != "in_meeting":
                    continue
                if p.get("leave_time"):
                    continue
                uid = str(p.get("user_id", ""))
                if not uid:
                    continue
                raw_name = p.get("user_name", "").strip()
                if not raw_name:
                    continue
                resolved = _db.resolve_display_name(raw_name)
                display = resolved["display_name"]
                count_it = resolved["count_enabled"]
                key = re.sub(r"\s+", "", display.lower())

                if key not in seen:
                    seen[key] = {
                        "name": display,
                        "raw_name": raw_name,
                        "user_id": uid,
                        "join_time": p.get("join_time", ""),
                        "status": p.get("status"),
                        "count_enabled": count_it,
                        "is_aliased": (display != raw_name),
                        "email": p.get("email", ""),
                    }
                else:
                    existing = seen[key]["join_time"]
                    new_jt = p.get("join_time", "")
                    if new_jt and (not existing or new_jt < existing):
                        seen[key]["join_time"] = new_jt

            participants = list(seen.values())

            for p in participants:
                jt = p.get("join_time", "")
                mins = 0
                disp = ""
                if jt:
                    try:
                        jd = datetime.fromisoformat(jt.replace("Z", "+00:00"))
                        mins = int((now_utc - jd).total_seconds() / 60)
                        disp = (
                            f"{mins//60}h{mins%60:02d}" if mins >= 60
                            else f"{mins}分钟"
                        )
                    except Exception:
                        pass
                p["online_minutes"] = mins
                p["online_display"] = disp

                # MYT display
                if jt:
                    try:
                        jd = datetime.fromisoformat(jt.replace("Z", "+00:00"))
                        p["join_time_display"] = jd.astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")
                    except:
                        p["join_time_display"] = jt[:16] if jt else ""
                else:
                    p["join_time_display"] = ""

            result["meetings"].append({
                "meeting_id": mid,
                "meeting_topic": topic,
                "participants": participants,
            })

            for p in participants:
                key = re.sub(r"\s+", "", p["name"].lower())
                if key not in all_canonical:
                    all_canonical.add(key)
                    if p["count_enabled"]:
                        enabled_count += 1

        result["total_online"] = enabled_count
        # Build online_list for frontend compatibility
        online_list = []
        for m in result.get("meetings", []):
            for p in m.get("participants", []):
                if p.get("status") == "in_meeting":
                    online_list.append(p)
        result["online_list"] = online_list
        # Build participants_summary from meetings participants (dedup by display_name)
        ps_map = {}
        for m in result.get("meetings", []):
            for p in m.get("participants", []):
                raw = p.get("name", "").strip()
                dn = _db.resolve_display_name(raw)["display_name"]
                key = dn.lower().replace(" ", "")
                if key not in ps_map:
                    ps_map[key] = {
                        "name": dn,
                        "is_online": p.get("status") == "in_meeting",
                        "last_active": p.get("join_time", ""),
                        "last_active_display": p.get("join_time_display", ""),
                        "total_actions": 1,
                        "duration_display": p.get("online_display", ""),
                        "flags": [],
                        "email": p.get("email", ""),
                        "meeting_id": p.get("meeting_id", ""),
                        "is_sharing": False,
                    }
                else:
                    ps_map[key]["total_actions"] += 1
        result["participants_summary"] = list(ps_map.values())
        _cache["live"] = {"data": result, "ts": now}
        return result

    async def _get_participants(self, meeting_id: str) -> list[dict]:
        all_p = []
        next_token = ""
        while True:
            params = {"page_size": 300}
            if next_token:
                params["next_page_token"] = next_token
            try:
                data = await self._get(
                    f"/metrics/meetings/{meeting_id}/participants", params
                )
                all_p.extend(data.get("participants", []))
                next_token = data.get("next_page_token", "")
                if not next_token:
                    break
            except Exception:
                break
        return all_p
