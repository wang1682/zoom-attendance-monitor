"""zoom.py — Unified Zoom data service.

    Six core methods covering all Zoom data needs:

    get_live_meetings
        Zoom Business Metrics API → merged online participants
        Returns meetings with participants, total online count, online list.
        Priority: Metrics API (Business) > webhook rebuild > DB fallback

    get_online_participants
        Flat list of currently online participants with metadata.
        Returns deduplicated list with display names, groups, sharing status.

    get_current_sharing
        Active sharing sessions (screen sharing right now).
        Returns currently sharing participants with details.

    get_recent_sharing
        Today's sharing history (active + completed within today).
        Returns deduped, sorted sharing records with group info.

    get_today_attendance_summary
        Per-participant today attendance: duration, enter/leave count, current status.

    get_meeting_state
        Live meeting states: meeting IDs, topics, participant counts, status.

    All methods accept tenant_id as first param and support both Request-based
    (via BaseService) and direct (tenant_id only) invocation.
"""

from __future__ import annotations

import re
import time
import sqlite3
import asyncio
from datetime import datetime, timezone, timedelta
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

from services.base import BaseService

# ── Constants ──────────────────────────────────────────────────────────────────
MYT = timezone(timedelta(hours=8))
METRICS_CACHE_TTL = 30.0

# ══════════════════════════════════════════════════════════════════════════════
# ZoomService
# ══════════════════════════════════════════════════════════════════════════════


class ZoomMetricsClient:
    """Internal Zoom Metrics API client — handles OAuth + caching."""

    def __init__(self, zoom_account: dict):
        self._account_id = zoom_account.get("account_id", "")
        self._client_id = zoom_account.get("client_id", "")
        self._client_secret = zoom_account.get("client_secret", "")
        self._token = ""
        self._expires_at = 0.0

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
        import httpx
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://zoom.us/oauth/token",
                data={"grant_type": "account_credentials", "account_id": self._account_id},
                auth=(self._client_id, self._client_secret),
            )
            r.raise_for_status()
            data = r.json()
            self._token = data["access_token"]
            self._expires_at = now + data["expires_in"]
            return self._token

    async def _get(self, path: str, params: dict | None = None) -> dict:
        import httpx
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
        """Fetch live meetings + participants from Zoom Metrics API."""
        import db
        meetings_data = await self._get("/metrics/meetings", {"type": "live", "page_size": 100})
        meetings_list = meetings_data.get("meetings", [])
        now_utc = datetime.now(timezone.utc)

        result = {"meetings": [], "total_online": 0}
        all_canonical: set[str] = set()
        enabled_count = 0

        async def _fetch_build(m):
            mid = str(m.get("id", ""))
            topic = m.get("topic", mid)
            raw_participants = await self._get_participants(mid)
            return mid, topic, raw_participants

        meeting_tasks = [_fetch_build(m) for m in meetings_list]
        fetched = await asyncio.gather(*meeting_tasks)

        for mid, topic, raw_participants in fetched:
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
                resolved = db.resolve_display_name(raw_name)
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
                        disp = f"{mins//60}h{mins%60:02d}" if mins >= 60 else f"{mins}分钟"
                    except Exception:
                        pass
                p["online_minutes"] = mins
                p["online_display"] = disp
                if jt:
                    try:
                        jd = datetime.fromisoformat(jt.replace("Z", "+00:00"))
                        p["join_time_display"] = jd.astimezone(MYT).strftime("%m-%d %H:%M:%S")
                    except Exception:
                        try:
                            _h, _m = int(jt[11:13]), int(jt[14:16])
                            _h8 = (_h + 8) % 24
                            p["join_time_display"] = f"{jt[5:10]} {_h8:02d}:{_m:02d}:00"
                        except Exception:
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
        online_list = []
        for m in result.get("meetings", []):
            for p in m.get("participants", []):
                if p.get("status") == "in_meeting":
                    online_list.append(p)
        result["online_list"] = online_list

        # Build participants_summary (dedup by display name)
        import db as _db
        ps_map = {}
        for m in result.get("meetings", []):
            for p in m.get("participants", []):
                raw = p.get("name", "").strip()
                dn = _db.resolve_display_name(raw)["display_name"]
                key = dn.lower().replace(" ", "")
                _now = datetime.now(timezone.utc)
                _jt = p.get("join_time", "")
                _lad = ""
                _myt = ""
                if _jt:
                    try:
                        _jd = datetime.fromisoformat(_jt.replace("Z", "+00:00"))
                        _secs = int((_now - _jd).total_seconds())
                        if _secs < 60:
                            _lad = "刚刚"
                        elif _secs < 3600:
                            _lad = str(_secs // 60) + "分钟前"
                        elif _secs < 86400:
                            _lad = str(_secs // 3600) + "小时前"
                        else:
                            _lad = _jd.astimezone(MYT).strftime("%m-%d %H:%M")
                        _myt = _jd.astimezone(MYT).strftime("%m-%d %H:%M:%S")
                    except Exception:
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
        return result

    async def _get_participants(self, meeting_id: str) -> list[dict]:
        all_p = []
        next_token = ""
        while True:
            params = {"page_size": 300}
            if next_token:
                params["next_page_token"] = next_token
            try:
                data = await self._get(f"/metrics/meetings/{meeting_id}/participants", params)
                all_p.extend(data.get("participants", []))
                next_token = data.get("next_page_token", "")
                if not next_token:
                    break
            except Exception:
                break
        return all_p


class ZoomService(BaseService):
    """Unified Zoom data service.

    Six core methods covering all Zoom data needs.
    Each method auto-selects the best data source based on tenant capabilities.

    Usage:
        zoom = ZoomService(request)
        live = await zoom.get_live_meetings("default")
        online = await zoom.get_online_participants("default")
        sharing = await zoom.get_current_sharing("default")
    """

    _metrics_cache: dict[str, dict] = {}  # tenant_id -> {"data": ..., "ts": ...}
    _metrics_clients: dict[str, ZoomMetricsClient] = {}  # account_id -> client

    # ── Internal helpers ──────────────────────────────────────────────────

    def _get_db(self) -> sqlite3.Connection:
        import db
        return db._get_conn()

    def _normalize(self, name: str) -> str:
        return re.sub(r"[\s_\-]+", "", name.strip().lower()) if name else ""

    def _myt_short(self, utc_str: str) -> str:
        """Convert UTC ISO string to MM-DD HH:MM MYT display."""
        if not utc_str:
            return ""
        try:
            dt = datetime.fromisoformat(utc_str.replace("Z", "+00:00"))
            return dt.astimezone(MYT).strftime("%m-%d %H:%M")
        except Exception:
            return utc_str[:16] if len(utc_str) >= 16 else utc_str

    async def _get_metrics_client(self, tenant_id: str) -> ZoomMetricsClient | None:
        """Get or create a ZoomMetricsClient for the tenant's active account."""
        import db
        tenant = db.get_tenant(tenant_id) if hasattr(db, "get_tenant") else {}
        if not tenant or not tenant.get("metrics_available", 0):
            return None
        accounts = db.get_zoom_accounts(tenant_id) if hasattr(db, "get_zoom_accounts") else []
        active = next(
            (a for a in accounts if a.get("is_active") and a.get("status") == "active"),
            None,
        )
        if not active:
            return None
        aid = active.get("account_id", "")
        if aid not in self._metrics_clients:
            self._metrics_clients[aid] = ZoomMetricsClient(active)
        return self._metrics_clients[aid]

    async def _fetch_metrics_live(self, tenant_id: str) -> dict | None:
        """Fetch live data from Zoom Metrics API. Returns None if unavailable."""
        now = time.time()
        cached = self._metrics_cache.get(tenant_id, {})
        if cached and now - cached.get("ts", 0) < METRICS_CACHE_TTL:
            return cached["data"]

        client = await self._get_metrics_client(tenant_id)
        if not client:
            return None

        try:
            data = await asyncio.wait_for(client.get_live(), timeout=10.0)
            if data and data.get("total_online", -1) >= 0:
                self._metrics_cache[tenant_id] = {"data": data, "ts": now}
                return data
        except (asyncio.TimeoutError, Exception):
            pass
        return None

    def _webhook_rebuild_online(self, tenant_id: str) -> dict:
        """Rebuild online state from zoom_participants webhook data."""
        conn = self._get_db()
        rows = conn.execute(
            "SELECT meeting_id, name, action, action_time "
            "FROM zoom_participants WHERE tenant_id=? AND action_time >= datetime('now', '-2 hours') "
            "ORDER BY meeting_id, name, action_time DESC",
            (tenant_id,),
        ).fetchall()

        online_map: dict[str, set] = {}
        for r in rows:
            mid = r["meeting_id"]
            name = r["name"]
            if mid not in online_map:
                online_map[mid] = set()
            if r["action"] in ("enter", "joined"):
                online_map[mid].add(name)
            elif r["action"] in ("leave", "left"):
                online_map[mid].discard(name)

        online_names: set[str] = set()
        for mid, names in online_map.items():
            for n in names:
                online_names.add(n)

        return {
            "online_count": len(online_names),
            "online_list": [{"name": n} for n in sorted(online_names)],
            "active_meetings": [
                {
                    "id": mid,
                    "topic": f"Meeting {mid[-6:]}",
                    "participant_count": len(names),
                }
                for mid, names in sorted(online_map.items(), key=lambda x: -len(x[1]))
                if names
            ],
            "source": "webhook_rebuild",
            "error": None,
        }

    def _db_fallback_online(self, tenant_id: str) -> dict:
        """Fallback: use db.get_current_online()."""
        import db
        try:
            data = db.get_current_online(tenant_id)
            return {
                "online_count": data.get("online_count", 0),
                "online_list": [{"name": n} for n in data.get("online_names", [])],
                "active_meetings": data.get("active_meetings", []),
                "source": "db_fallback",
                "error": None,
            }
        except Exception as e:
            return {
                "online_count": 0,
                "online_list": [],
                "active_meetings": [],
                "source": "db_fallback_error",
                "error": str(e),
            }

    def _enrich_online_list(self, online_list: list[dict], tenant_id: str) -> list[dict]:
        """Add display_name, group_name, group_id to each online entry."""
        import db
        enriched = []
        for entry in online_list:
            raw_name = entry.get("name", "")
            resolved = db.resolve_display_name(raw_name) if hasattr(db, "resolve_display_name") else {}
            display = resolved.get("display_name", raw_name)
            enriched.append({
                **entry,
                "display_name": display,
                "group_name": resolved.get("group_name", ""),
                "group_id": resolved.get("group_id", ""),
                "count_enabled": resolved.get("count_enabled", True),
            })
        return enriched

    # ══════════════════════════════════════════════════════════════════════
    # Public API — 6 core methods
    # ══════════════════════════════════════════════════════════════════════

    async def get_live_meetings(self, tenant_id: str) -> dict:
        """获取当前在线会议列表及参与者（统一来源）。

        Returns:
            meetings: list[dict] — each with meeting_id, topic, participants
            total_online: int
            online_list: list[dict]
            participants_summary: list[dict]
            source: "zoom_metrics" | "webhook_rebuild" | "db_fallback"
            error: str | None
        """
        import db
        result = {
            "meetings": [],
            "total_online": 0,
            "online_list": [],
            "participants_summary": [],
            "source": "db_fallback",
            "error": None,
        }

        tenant = db.get_tenant(tenant_id) if hasattr(db, "get_tenant") else {}
        metrics_available = (tenant or {}).get("metrics_available", 0)

        # Step 1: Metrics API (Business 租户)
        if metrics_available:
            try:
                metrics_data = await self._fetch_metrics_live(tenant_id)
                if metrics_data:
                    result["meetings"] = metrics_data.get("meetings", [])
                    result["total_online"] = metrics_data.get("total_online", 0)
                    result["online_list"] = metrics_data.get("online_list", [])
                    result["participants_summary"] = metrics_data.get("participants_summary", [])
                    result["source"] = "zoom_metrics"
                    return result
            except Exception as e:
                result["error"] = f"Metrics API failed: {e}"

        # Step 2: Webhook rebuild (Pro + Business fallback)
        try:
            webhook = self._webhook_rebuild_online(tenant_id)
            if webhook["online_count"] > 0 or result["error"]:
                result["total_online"] = webhook["online_count"]
                result["online_list"] = webhook["online_list"]
                result["active_meetings"] = webhook["active_meetings"]
                result["source"] = "webhook_rebuild"
                result["error"] = result.get("error") or webhook.get("error")
                # Build participants_summary from webhook data
                enriched = self._enrich_online_list(webhook["online_list"], tenant_id)
                result["participants_summary"] = [
                    {
                        "name": e.get("display_name", e.get("name", "")),
                        "is_online": True,
                        "last_active": "",
                        "last_active_display": "",
                        "last_active_myt": "",
                        "total_actions": 0,
                        "duration_display": "",
                        "flags": [],
                        "email": "",
                        "meeting_id": "",
                        "is_sharing": False,
                    }
                    for e in enriched
                ]
                return result
        except Exception as e:
            result["error"] = f"Webhook rebuild failed: {e}"

        # Step 3: DB fallback
        if result["total_online"] == 0:
            try:
                fallback = self._db_fallback_online(tenant_id)
                result["total_online"] = fallback["online_count"]
                result["online_list"] = fallback["online_list"]
                result["source"] = fallback["source"]
                result["error"] = fallback["error"]
            except Exception as e:
                result["error"] = f"DB fallback failed: {e}"

        return result

    async def get_online_participants(self, tenant_id: str) -> dict:
        """获取当前在线参与者列表（带分组/显示名等信息）。

        Returns:
            online_count: int
            participants: list[dict] — each with name, display_name, meeting_id,
                           group_name, group_id, is_sharing, join_time, email
            active_meetings: list[dict]
            source: str
        """
        import db
        live = await self.get_live_meetings(tenant_id)

        if live["source"] == "zoom_metrics":
            participants = []
            for m in live.get("meetings", []):
                for p in m.get("participants", []):
                    resolved = db.resolve_display_name(p.get("name", ""))
                    participants.append({
                        "name": p.get("name", ""),
                        "display_name": resolved.get("display_name", p.get("name", "")),
                        "raw_name": p.get("raw_name", ""),
                        "meeting_id": m.get("meeting_id", ""),
                        "meeting_topic": m.get("meeting_topic", ""),
                        "group_name": resolved.get("group_name", ""),
                        "group_id": resolved.get("group_id", ""),
                        "count_enabled": resolved.get("count_enabled", True),
                        "is_sharing": p.get("is_sharing", False),
                        "sharing_content": p.get("sharing_content", ""),
                        "join_time": p.get("join_time", ""),
                        "join_time_display": p.get("join_time_display", ""),
                        "online_minutes": p.get("online_minutes", 0),
                        "online_display": p.get("online_display", ""),
                        "email": p.get("email", ""),
                        "user_id": p.get("user_id", ""),
                    })
            return {
                "online_count": live.get("total_online", 0),
                "participants": participants,
                "active_meetings": [
                    {
                        "id": m.get("meeting_id", ""),
                        "topic": m.get("meeting_topic", ""),
                        "participant_count": len(m.get("participants", [])),
                    }
                    for m in live.get("meetings", [])
                ],
                "source": live["source"],
            }

        # Webhook / DB fallback — enrich from online_list
        enriched = self._enrich_online_list(live.get("online_list", []), tenant_id)
        return {
            "online_count": live.get("total_online", 0),
            "participants": enriched,
            "active_meetings": live.get("active_meetings", []),
            "source": live["source"],
        }

    async def get_current_sharing(self, tenant_id: str) -> list[dict]:
        """获取当前正在共享屏幕的参与者列表。

        Returns list of:
            name: str (display name)
            raw_name: str
            meeting_id: str
            meeting_topic: str
            content: str
            start_time: str
            duration_minutes: int
            group_name: str
            group_id: str
        """
        import db
        import sys
        live = await self.get_live_meetings(tenant_id)
        now_utc = datetime.now(timezone.utc)
        sharers = []

        # ── 1. 从 Metrics 收集（有 is_sharing 标记的） ──
        for m in live.get("meetings", []):
            for p in m.get("participants", []):
                if p.get("is_sharing"):
                    resolved = db.resolve_display_name(p.get("name", ""))
                    jt = p.get("join_time", "")
                    mins = 0
                    if jt:
                        try:
                            jd = datetime.fromisoformat(jt.replace("Z", "+00:00"))
                            mins = int((now_utc - jd).total_seconds() / 60)
                        except Exception:
                            pass
                    sharers.append({
                        "name": p.get("name", ""),
                        "raw_name": p.get("raw_name", ""),
                        "display_name": resolved.get("display_name", p.get("name", "")),
                        "meeting_id": m.get("meeting_id", ""),
                        "meeting_topic": m.get("meeting_topic", ""),
                        "content": p.get("sharing_content", ""),
                        "start_time": p.get("join_time", ""),
                        "start_time_display": p.get("join_time_display", ""),
                        "duration_minutes": mins,
                        "duration_display": p.get("online_display", ""),
                        "group_name": resolved.get("group_name", ""),
                        "group_id": resolved.get("group_id", ""),
                        "user_id": p.get("user_id", ""),
                        "email": p.get("email", ""),
                    })

        # ── 2. sharing_live fallback：补充 Metrics 没有的 ──
        #    与在线名单交叉校验：已不在线的标记过期
        try:
            conn = self._get_db()

            # 收集当前在线 participants：user_name / user_id 集合
            online_names = set()
            online_ids = set()
            for m in live.get("meetings", []):
                for p in m.get("participants", []):
                    nm = (p.get("name") or "").strip().lower()
                    uid = (p.get("user_id") or "").strip()
                    if nm:
                        online_names.add(nm)
                    if uid:
                        online_ids.add(uid)

            # 查 sharing_live 并交叉校验
            rows = conn.execute(
                "SELECT sl.*, COALESCE(mg.name, '') AS group_name, COALESCE(md.group_id, '') AS group_id "
                "FROM sharing_live sl "
                "LEFT JOIN member_display md ON (md.raw_name=sl.user_name OR md.display_name=sl.user_name) AND md.tenant_id=sl.tenant_id "
                "LEFT JOIN member_groups mg ON mg.id=md.group_id AND mg.tenant_id=md.tenant_id "
                "WHERE sl.tenant_id=? AND sl.is_active=1 "
                "ORDER BY sl.start_time DESC",
                (tenant_id,),
            ).fetchall()

            seen_raw = {s.get("raw_name", "").lower() for s in sharers}
            now_str_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            stale_threshold_utc = datetime.now(timezone.utc) - timedelta(hours=6)
            stale_ids = []  # 需要标记过期的 sharing_live.id 列表

            for r in rows:
                d = dict(r)
                raw_lower = (d.get("user_name") or "").lower()
                user_id = (d.get("user_id") or "").strip()

                # 是否在线：name 或 user_id 任一匹配
                is_online = raw_lower in online_names or user_id in online_ids

                # 不在线的 → 标记 stale，不展示
                if not is_online:
                    rid = d.get("id")
                    if rid:
                        stale_ids.append(rid)
                    continue

                # 已在 Metrics 中 → 跳过
                if raw_lower in seen_raw:
                    continue

                # 超过 6h 标记过期
                if d.get("start_time"):
                    try:
                        st_dt = datetime.fromisoformat(d["start_time"].replace("Z", "+00:00"))
                        if st_dt < stale_threshold_utc:
                            rid = d.get("id")
                            if rid:
                                stale_ids.append(rid)
                            continue
                    except Exception:
                        pass

                resolved = db.resolve_display_name(d.get("user_name", ""))
                st = d.get("start_time", "")
                mins = 0
                if st:
                    try:
                        sd = datetime.fromisoformat(st.replace("Z", "+00:00"))
                        mins = int((now_utc - sd).total_seconds() / 60)
                    except Exception:
                        pass

                sharers.append({
                    "name": resolved.get("display_name", d.get("user_name", "")),
                    "raw_name": d.get("user_name", ""),
                    "display_name": resolved.get("display_name", d.get("user_name", "")),
                    "meeting_id": d.get("meeting_id", ""),
                    "meeting_topic": d.get("meeting_topic", ""),
                    "content": d.get("content", ""),
                    "start_time": st,
                    "start_time_display": self._myt_short(st),
                    "duration_minutes": mins,
                    "duration_display": f"{mins}分钟" if mins < 60 else f"{mins//60}h{mins%60:02d}",
                    "group_name": d.get("group_name", ""),
                    "group_id": d.get("group_id", ""),
                    "user_id": "",
                    "email": "",
                })

            # ── 批量标记 stale（只清理，不补推，避免和 webhook ended 重复） ──
            if stale_ids:
                placeholders = ",".join("?" for _ in stale_ids)
                conn.execute(
                    f"UPDATE sharing_live SET is_active=0, end_time=? WHERE id IN ({placeholders})",
                    (now_str_utc, *stale_ids),
                )
                conn.commit()
        except Exception:
            pass

        return sharers

    async def get_recent_sharing(
        self,
        tenant_id: str,
        limit: int = 200,
        start_time: str | None = None,
        end_time: str | None = None,
        search: str | None = None,
        group_id: str | None = None,
    ) -> dict:
        """获取今日共享记录（统一来源）。

        Returns:
            items: list[dict] — deduped, enriched, sorted
            total: int
            meta: dict
            source: str
        """
        import db
        # Delegate to db.get_sharing_records (already has dedup + stale filter)
        items, total, meta = db.get_sharing_records(
            tenant_id=tenant_id,
            limit=limit,
            start_time=start_time,
            end_time=end_time,
            search=search,
            group_id=group_id,
        )

        # Enrich with display info
        for item in items:
            raw_name = item.get("user_name", "")
            resolved = db.resolve_display_name(raw_name)
            item["display_name"] = resolved.get("display_name", raw_name)
            item["group_name"] = item.get("group_name", "") or resolved.get("group_name", "")

        # Sort: active first, then by latest start, then longest
        now_utc = datetime.now(timezone.utc)

        def _sort_key(item: dict) -> tuple:
            is_active = 0 if item.get("is_active") else 1
            first_start_str = item.get("start_time", "")
            first_start_ts = 0.0
            if first_start_str:
                try:
                    dt = datetime.fromisoformat(first_start_str.replace("Z", "+00:00"))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=timezone.utc)
                    first_start_ts = dt.timestamp()
                except Exception:
                    pass
            total_sec = item.get("total_seconds", 0) or item.get("duration_seconds", 0)
            return (is_active, -first_start_ts, -total_sec)

        items.sort(key=_sort_key)

        return {
            "items": items,
            "total": total,
            "meta": meta,
            "source": "zoom_service.recent_sharing",
        }

    async def get_today_attendance_summary(self, tenant_id: str) -> dict:
        """获取今日参会汇总（统一来源）。

        Returns per-participant: duration, enter/leave count, current status.
        Delegates to db.get_today_attendance_summary for the heavy lifting.
        """
        import db
        return db.get_today_attendance_summary(tenant_id=tenant_id)

    async def get_meeting_state(self, tenant_id: str) -> dict:
        """获取当前会议状态（统一来源）。

        Returns:
            meetings: list[dict] — meeting_id, topic, participant_count, status
            error: str | None
            source: str
        """
        live = await self.get_live_meetings(tenant_id)

        meetings = []
        if live["source"] == "zoom_metrics":
            for m in live.get("meetings", []):
                meetings.append({
                    "meeting_id": m.get("meeting_id", ""),
                    "topic": m.get("meeting_topic", ""),
                    "participant_count": len(m.get("participants", [])),
                    "status": "live",
                })
        else:
            for m in live.get("active_meetings", []):
                meetings.append({
                    "meeting_id": m.get("id", ""),
                    "topic": m.get("topic", ""),
                    "participant_count": m.get("participant_count", 0),
                    "status": "live",
                })

        return {
            "meetings": meetings,
            "total_meetings": len(meetings),
            "source": live["source"],
            "error": live.get("error"),
        }
