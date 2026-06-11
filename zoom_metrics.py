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

_cache = {}  # account_id -> {"data": ..., "ts": ...}
CACHE_TTL = 30


class ZoomMetrics:
    def __init__(self, zoom_account: dict | None = None):
        self._token = ""
        self._expires_at = 0.0
        if zoom_account:
            self._account_id = zoom_account.get("account_id", settings.zoom_account_id)
            self._client_id = zoom_account.get("client_id", settings.zoom_client_id)
            self._client_secret = zoom_account.get("client_secret", settings.zoom_client_secret)
        else:
            self._account_id = settings.zoom_account_id
            self._client_id = settings.zoom_client_id
            self._client_secret = settings.zoom_client_secret

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://zoom.us/oauth/token",
                data={"grant_type": "account_credentials",
                      "account_id": self._account_id},
                auth=(self._client_id, self._client_secret),
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
        account_cache = _cache.get(self._account_id, {"data": None, "ts": 0})
        if account_cache["data"] and now - account_cache["ts"] < CACHE_TTL:
            return account_cache["data"]

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

                is_sharing = bool(
                    p.get("share_application") or
                    p.get("share_desktop") or
                    p.get("share_whiteboard")
                )
                sharing_content = ""
                if is_sharing:
                    if p.get("share_application"):
                        sharing_content = "application"
                    elif p.get("share_desktop"):
                        sharing_content = "desktop"
                    elif p.get("share_whiteboard"):
                        sharing_content = "whiteboard"
                if key not in seen:
                    seen[key] = {
                        "name": display,
                        "raw_name": raw_name,
                        "meeting_id": mid,
                        "meeting_topic": topic,
                        "user_id": uid,
                        "join_time": p.get("join_time", ""),
                        "status": p.get("status"),
                        "count_enabled": count_it,
                        "is_aliased": (display != raw_name),
                        "email": p.get("email", ""),
                        "is_sharing": is_sharing,
                        "sharing_content": sharing_content,
                    }
                else:
                    # Merge: if any dedup entry is sharing, mark as sharing
                    if is_sharing:
                        seen[key]["is_sharing"] = True
                        if not seen[key].get("sharing_content"):
                            seen[key]["sharing_content"] = sharing_content
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
                        # fallback: try simple hour offset
                        try:
                            _h, _m = int(jt[11:13]), int(jt[14:16])
                            _h8 = (_h + 8) % 24
                            p["join_time_display"] = f"{jt[5:10]} {_h8:02d}:{_m:02d}:00"
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
                from datetime import datetime as _dt, timezone as _tz, timedelta as _td
                _MYT = _tz(_td(hours=8))
                _now = _dt.now(_tz.utc)
                _jt = p.get("join_time", "")
                _lad = ""
                _myt = ""
                if _jt:
                    try:
                        _jd = _dt.fromisoformat(_jt.replace("Z", "+00:00"))
                        _secs = int((_now - _jd).total_seconds())
                        if _secs < 60: _lad = "刚刚"
                        elif _secs < 3600: _lad = str(_secs // 60) + "分钟前"
                        elif _secs < 86400: _lad = str(_secs // 3600) + "小时前"
                        else: _lad = _jd.astimezone(_MYT).strftime("%m-%d %H:%M")
                        _myt = _jd.astimezone(_MYT).strftime("%m-%d %H:%M:%S")
                    except:
                        _lad = _jt[:16]
                        _myt = _jt[:16]
                if key not in ps_map:
                    ps_map[key] = {
                        "name": dn,
                        "is_online": p.get("status") == "in_meeting",
                        "last_active": _jt,
                        "last_active_display": _lad,
                        "last_active_myt": _myt,
                        "total_actions": 1,
                        "duration_display": p.get("online_display", ""),
                        "flags": [],
                        "email": p.get("email", ""),
                        "meeting_id": p.get("meeting_id", ""),
                        "is_sharing": p.get("is_sharing", False),
                    }
                else:
                    ps_map[key]["total_actions"] += 1
        result["participants_summary"] = list(ps_map.values())
        _cache[self._account_id] = {"data": result, "ts": now}
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
