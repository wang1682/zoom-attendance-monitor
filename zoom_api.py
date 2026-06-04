"""
zoom_api.py — Zoom Server-to-Server OAuth 客户端
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from config import settings

MYT = timezone(timedelta(hours=8))


class ZoomAPI:
    def __init__(self):
        self._token: str = ""
        self._expires_at: float = 0
        self._scheduled_cache: dict = {"ids": [], "cached_at": 0}

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(
                "https://zoom.us/oauth/token",
                data={"grant_type": "account_credentials", "account_id": settings.zoom_account_id},
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

    async def get_participants(self, meeting_id: str) -> list[dict]:
        all_p = []
        next_token = ""
        while True:
            params = {"page_size": 300}
            if next_token:
                params["next_page_token"] = next_token
            result = await self._get(f"/report/meetings/{meeting_id}/participants", params)
            all_p.extend(result.get("participants", []))
            next_token = result.get("next_page_token", "")
            if not next_token:
                break
        return all_p

    async def get_scheduled_meetings(self) -> list[str]:
        now = time.time()
        if self._scheduled_cache["ids"] and now - self._scheduled_cache["cached_at"] < 300:
            return self._scheduled_cache["ids"]
        try:
            result = await self._get("/users/me/meetings", {"type": "scheduled", "page_size": 300})
            ids = [str(m["id"]) for m in result.get("meetings", [])]
            self._scheduled_cache["ids"] = ids
            self._scheduled_cache["cached_at"] = now
            return ids
        except Exception:
            return []

    async def get_user_id(self) -> Optional[str]:
        try:
            result = await self._get("/users/me")
            return result.get("id")
        except Exception:
            return None

    @staticmethod
    def parse_zoom_utc(utc_str: str) -> Optional[datetime]:
        """解析 Zoom API 的 UTC 字符串 → UTC aware datetime"""
        try:
            dt = datetime.strptime(utc_str.replace("Z", "+0000"), "%Y-%m-%dT%H:%M:%S%z")
            return dt
        except Exception:
            return None
