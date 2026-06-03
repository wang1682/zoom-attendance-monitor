"""
zoom_metrics.py — Zoom Business Metrics API 服务层
支持 member_aliases 别名映射和去重
"""
from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx
from config import settings
import db as _db

_cache = {"live": {"data": None, "ts": 0}, "aliases": {"data": {}, "ts": 0}}
CACHE_TTL = 30
ALIAS_CACHE_TTL = 60


def _load_aliases() -> dict:
    """加载别名映射：alias_name -> {canonical_name, count_enabled}"""
    now = time.time()
    ac = _cache["aliases"]
    if ac["data"] and now - ac["ts"] < ALIAS_CACHE_TTL:
        return ac["data"]
    conn = _db._get_conn()
    rows = conn.execute(
        "SELECT alias_name, canonical_name, count_enabled FROM member_aliases"
    ).fetchall()
    mapping = {}
    for alias, canonical, enabled in rows:
        mapping[alias.strip().lower().replace(" ", "")] = {
            "canonical": canonical.strip(),
            "enabled": bool(enabled),
        }
    ac["data"] = mapping
    ac["ts"] = now
    return mapping


def _normalize(name: str) -> str:
    """基础归一化：trim + lowercase + 去空格"""
    return name.strip().lower().replace(" ", "")


def _resolve_name(raw_name: str, aliases: dict) -> tuple:
    """返回 (canonical_name, count_enabled, is_aliased)"""
    key = _normalize(raw_name)
    if key in aliases:
        a = aliases[key]
        return a["canonical"], a["enabled"], True
    return raw_name.strip(), True, False


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
        """获取在线数据，按 canonical_name 去重，支持别名"""
        now = time.time()
        cached = _cache["live"]
        if cached["data"] and now - cached["ts"] < CACHE_TTL:
            return cached["data"]

        meetings_data = await self._get(
            "/metrics/meetings", {"type": "live", "page_size": 100}
        )
        meetings_list = meetings_data.get("meetings", [])
        aliases = _load_aliases()
        now_utc = datetime.now(timezone.utc)

        result = {"meetings": [], "total_online": 0}
        all_canonical = set()
        enabled_count = 0

        for m in meetings_list:
            mid = str(m.get("id", ""))
            topic = m.get("topic", mid)

            raw_participants = await self._get_participants(mid)

            # 过滤：in_meeting + 无 leave_time
            # 按 canonical_name 去重
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
                canonical, count_it, is_aliased = _resolve_name(raw_name, aliases)
                key = _normalize(canonical)

                if key not in seen:
                    seen[key] = {
                        "name": canonical,
                        "raw_name": raw_name,
                        "user_id": uid,
                        "join_time": p.get("join_time", ""),
                        "status": p.get("status"),
                        "count_enabled": count_it,
                        "is_aliased": is_aliased,
                        "email": p.get("email", ""),
                    }
                else:
                    # 同名：保留最早的 join_time
                    existing = seen[key]["join_time"]
                    new_jt = p.get("join_time", "")
                    if new_jt and (not existing or new_jt < existing):
                        seen[key]["join_time"] = new_jt

            participants = list(seen.values())

            # 计算在线时长
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

            result["meetings"].append({
                "meeting_id": mid,
                "meeting_topic": topic,
                "participants": participants,
            })

            for p in participants:
                key = _normalize(p["name"])
                if key not in all_canonical:
                    all_canonical.add(key)
                    if p["count_enabled"]:
                        enabled_count += 1

        result["total_online"] = enabled_count
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
