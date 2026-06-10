"""
zoom_api.py — Zoom Server-to-Server OAuth 客户端
支持传参实例化，不传则默认读 .env 配置
"""
from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import Optional

import httpx
from config import settings

MYT = timezone(timedelta(hours=8))


class ZoomAPI:
    def __init__(self, account_id: str = None, client_id: str = None,
                 client_secret: str = None, tenant_id: str = None):
        self._token: str = ""
        self._expires_at: float = 0
        self._scheduled_cache: dict = {"ids": [], "cached_at": 0}
        self._account_id = account_id or settings.zoom_account_id
        self._client_id = client_id or settings.zoom_client_id
        self._client_secret = client_secret or settings.zoom_client_secret
        self.tenant_id = tenant_id or "default"
        self.label = ""

    async def _get_token(self) -> str:
        now = time.time()
        if self._token and now < self._expires_at - 60:
            return self._token
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

    async def test_connection(self) -> dict:
        """测试 Zoom API 连接，返回详细诊断结果"""
        result = {
            "ok": False,
            "token": False,
            "user": {},
            "scopes": [],
            "meetings": {"scheduled": 0, "recent": 0},
            "participants_ok": False,
            "error": None,
        }
        # Step 1: get token
        try:
            token = await self._get_token()
            result["token"] = True
        except Exception as e:
            result["error"] = f"Token: {e}"
            return result

        # Step 2: get user info
        try:
            user_data = await self._get("/users/me")
            result["user"] = {
                "id": user_data.get("id", ""),
                "email": user_data.get("email", ""),
                "display_name": user_data.get("display_name", ""),
                "account_id": user_data.get("account_id", ""),
                "plan_type": user_data.get("plan_type", 0),
            }
        except Exception as e:
            result["error"] = f"User: {e}"
            return result

        # Step 3: check scopes by calling dashboard API
        try:
            # Try to list meetings as scope check
            meetings_data = await self._get("/users/me/meetings", {"page_size": 1, "type": "scheduled"})
            result["meetings"]["scheduled"] = len(meetings_data.get("meetings", []))
        except Exception as e:
            result["error"] = f"Meetings scope: {e}"
            return result

        # Step 4: try to read past meeting participants (report scope)
        try:
            past = await self._get("/users/me/meetings", {"page_size": 1, "type": "ended"})
            past_meetings = past.get("meetings", [])
            result["meetings"]["recent"] = len(past_meetings)
            if past_meetings:
                mid = str(past_meetings[0]["id"])
                participants = await self.get_participants(mid)
                result["participants_ok"] = len(participants) > 0
        except Exception:
            # Report scope may not be available - not critical
            pass

        result["ok"] = True
        return result
