"""
MemberService — 统一成员、别名、显示名、在线状态

所有成员相关数据的统一入口。
核心原则：所有查询/写入按 tenant_id 隔离，防止跨租户污染。

依赖：db.py（底层 SQLite），ZoomService（在线状态）
"""

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import db  # 底层 SQLite 操作

# ── 内置缓存 ──────────────────────────────────
_display_cache: dict[str, Any] = {"mapping": {}, "ts": 0, "tenant_id": None}
_alias_cache: dict[str, Any] = {"aliases": [], "ts": 0, "tenant_id": None}


class MemberService:
    """成员管理服务"""

    # ──────────────────────────────────────────
    # 显示名解析
    # ──────────────────────────────────────────

    def resolve_display(self, raw_name: str, tenant_id: str | None = None) -> dict:
        """
        返回 {display_name, count_enabled, raw_name, group_name}
        统一解析：raw_name → display_name，带 30 秒本地缓存。
        """
        now = time.time()
        mapping = _display_cache["mapping"]
        if (
            not mapping
            or now - _display_cache["ts"] > 30
            or _display_cache.get("tenant_id") != tenant_id
        ):
            conn = db._get_conn()
            if tenant_id:
                rows = conn.execute(
                    "SELECT md.raw_name, md.display_name, md.match_key, md.count_enabled, "
                    "COALESCE(md.aliases, '[]'), g.name AS group_name "
                    "FROM member_display md "
                    "LEFT JOIN member_groups g ON g.id = md.group_id AND g.tenant_id = md.tenant_id "
                    "WHERE md.tenant_id = ?",
                    (tenant_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT md.raw_name, md.display_name, md.match_key, md.count_enabled, "
                    "COALESCE(md.aliases, '[]'), g.name AS group_name "
                    "FROM member_display md "
                    "LEFT JOIN member_groups g ON g.id = md.group_id AND g.tenant_id = md.tenant_id"
                ).fetchall()
            _display_cache["mapping"] = {
                r[0]: {
                    "display": r[1],
                    "key": r[2],
                    "enabled": bool(r[3]),
                    "aliases": json.loads(r[4] or "[]"),
                    "group": r[5] or "",
                }
                for r in rows
            }
            _display_cache["ts"] = now
            _display_cache["tenant_id"] = tenant_id
            mapping = _display_cache["mapping"]

        name = raw_name.strip()
        if not name:
            return {"display_name": "", "count_enabled": True, "raw_name": name, "group_name": ""}

        # 1. Exact match on raw_name
        if name in mapping:
            m = mapping[name]
            return {"display_name": m["display"], "count_enabled": m["enabled"],
                    "raw_name": name, "group_name": m["group"], "is_configured": True}

        # 2. Match on match_key (lowercase, no spaces)
        key = re.sub(r"\s+", "", name.lower())
        for raw, m in mapping.items():
            if m["key"] == key:
                return {"display_name": m["display"], "count_enabled": m["enabled"],
                        "raw_name": name, "group_name": m["group"], "is_configured": True}

        # 3. Match on aliases
        name_lower = name.lower().replace(" ", "")
        for raw, m in mapping.items():
            alias_list = [a.lower().replace(" ", "") for a in m.get("aliases", [])]
            if name_lower in alias_list:
                return {"display_name": m["display"], "count_enabled": m["enabled"],
                        "raw_name": name, "group_name": m["group"], "is_configured": True}

        # 4. Fallback — try dedup with same match_key
        for raw, m in mapping.items():
            if m["key"] == key:
                return {"display_name": m["display"], "count_enabled": m["enabled"],
                        "raw_name": name, "group_name": m["group"], "is_configured": True}

        # 5. Last resort — not configured
        return {"display_name": name, "count_enabled": True, "raw_name": name, "group_name": "", "is_configured": False}

    # ──────────────────────────────────────────
    # 成员列表（含在线状态合并）
    # ──────────────────────────────────────────

    def get_member_list(self, tenant_id: str) -> list[dict]:
        """
        获取成员完整列表，含在线状态、分组、今日累计。
        返回 [{name, display_name, group, is_online, today_count, online_since, ...}]
        """
        conn = db._get_conn()
        rows = conn.execute(
            "SELECT md.raw_name, md.display_name, md.count_enabled, "
            "COALESCE(g.name, '') AS group_name, "
            "md.created_at "
            "FROM member_display md "
            "LEFT JOIN member_groups g ON g.id = md.group_id AND g.tenant_id = md.tenant_id "
            "WHERE md.tenant_id = ? "
            "ORDER BY g.name, md.display_name",
            (tenant_id,),
        ).fetchall()

        members = []
        for r in rows:
            members.append({
                "raw_name": r[0],
                "display_name": r[1],
                "count_enabled": bool(r[2]),
                "group_name": r[3],
                "created_at": r[4],
            })
        return members

    # ──────────────────────────────────────────
    # 在线状态合并（结合 ZoomService）
    # ──────────────────────────────────────────

    async def get_members_with_online(
        self, tenant_id: str, online_participants: dict[str, list] | None = None
    ) -> list[dict]:
        """
        合并成员列表 + 在线参与者数据。
        online_participants 由外部 ZoomService 传入，避免重复调用。
        """
        import asyncio
        from services.zoom import ZoomService

        members = self.get_member_list(tenant_id)

        if online_participants is None:
            zoom = ZoomService()
            live = await zoom.get_live_meetings(tenant_id)
            online_raw = live.get("participants", [])
        else:
            online_raw = online_participants

        # 解析在线状态
        online_map = {}
        for op in online_raw:
            display_info = self.resolve_display(op.get("user_name", ""), tenant_id)
            normalized = display_info["display_name"].lower().replace(" ", "")
            online_map[normalized] = op

        # 合并
        result = []
        
        # 预先获取共享状态
        sharing_active_names = set()
        try:
            zoom = ZoomService()
            sharing_list = await zoom.get_current_sharing(tenant_id)
            sharing_active_names = {
                s.get("user_name", "").lower().replace(" ", "")
                for s in sharing_list if isinstance(s, dict)
            }
        except Exception:
            sharing_active_names = set()
        
        for m in members:
            normalized = m["display_name"].lower().replace(" ", "")
            op = online_map.get(normalized)
            if op:
                m["is_online"] = True
                m["online_since"] = op.get("join_time", "")
                m["meeting_id"] = op.get("meeting_id", "")
                m["meeting_topic"] = op.get("meeting_topic", "")
                m["meeting_url"] = op.get("meeting_url", "")
                m["is_sharing"] = normalized in sharing_active_names
            else:
                m["is_online"] = False
                m["online_since"] = ""
                m["meeting_id"] = ""
                m["meeting_topic"] = ""
                m["meeting_url"] = ""
                m["is_sharing"] = False

            result.append(m)

        # 在线成员排前面，按在线时间降序
        result.sort(key=lambda x: (not x["is_online"], x.get("online_since", "") or ""), reverse=True)
        # 再按分组名排序（离线的）
        result.sort(key=lambda x: (not x["is_online"], x.get("group_name", "") or ""))

        return result

    # ──────────────────────────────────────────
    # 成员今日累计
    # ──────────────────────────────────────────

    def get_today_attendance(self, tenant_id: str) -> list[dict]:
        """
        获取本租户所有成员的今日出勤统计。
        返回 [{name, today_count, first_entry, last_activity, total_duration}]
        """
        conn = db._get_conn()
        myt_today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        rows = conn.execute(
            """
            SELECT
                COALESCE(md.display_name, zp.name) AS display_name,
                COUNT(*) AS action_count,
                MIN(zp.action_time) AS first_entry,
                MAX(zp.action_time) AS last_activity
            FROM zoom_participants zp
            LEFT JOIN member_display md
                ON REPLACE(LOWER(TRIM(zp.name)), ' ', '') = REPLACE(LOWER(TRIM(md.match_key)), ' ', '')
                AND md.tenant_id = ?
            WHERE zp.created_at >= ? || ' 00:00:00'
            AND zp.tenant_id = ?
            GROUP BY COALESCE(md.display_name, zp.name)
            ORDER BY action_count DESC
            """,
            (tenant_id, myt_today, tenant_id),
        ).fetchall()

        return [
            {
                "display_name": r[0],
                "today_count": r[1] or 0,
                "first_entry": r[2] or "",
                "last_activity": r[3] or "",
            }
            for r in rows
        ]

    # ──────────────────────────────────────────
    # 别名管理
    # ──────────────────────────────────────────

    def get_aliases(self, tenant_id: str | None = None) -> list[dict]:
        """获取别名列表（通过 member_display.aliases JSON 字段）"""
        conn = db._get_conn()
        if tenant_id:
            rows = conn.execute(
                "SELECT id, raw_name AS canonical_name, display_name AS alias_name, "
                "count_enabled, COALESCE(aliases, '[]') AS note, created_at, updated_at "
                "FROM member_display WHERE tenant_id = ? ORDER BY display_name",
                (tenant_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, raw_name AS canonical_name, display_name AS alias_name, "
                "count_enabled, COALESCE(aliases, '[]') AS note, created_at, updated_at "
                "FROM member_display ORDER BY display_name"
            ).fetchall()
        return [dict(r) for r in rows]

    def add_alias(
        self, canonical_name: str, alias_name: str, tenant_id: str,
        note: str = "", count_enabled: bool = True
    ) -> dict:
        """添加别名（写入 member_display.aliases JSON）"""
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        try:
            # 查找 canonical 在 member_display 中的记录
            existing = conn.execute(
                "SELECT id, aliases FROM member_display "
                "WHERE display_name = ? AND tenant_id = ?",
                (canonical_name.strip(), tenant_id),
            ).fetchone()
            if existing:
                # 已有的记录，往 aliases JSON 追加
                aliases = json.loads(existing[1] or "[]")
                if alias_name.strip() not in aliases:
                    aliases.append(alias_name.strip())
                    conn.execute(
                        "UPDATE member_display SET aliases = ?, updated_at = ? WHERE id = ?",
                        (json.dumps(aliases), now, existing[0]),
                    )
            else:
                # 新 canonical 成员
                match_key = canonical_name.strip().lower().replace(" ", "")
                aliases = json.dumps([alias_name.strip()])
                conn.execute(
                    "INSERT INTO member_display (raw_name, display_name, match_key, "
                    "count_enabled, aliases, tenant_id, created_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (canonical_name.strip(), canonical_name.strip(), match_key,
                     1 if count_enabled else 0, aliases, tenant_id, now, now),
                )
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_alias(self, alias_id: int, tenant_id: str | None = None) -> dict:
        """删除别名（通过 member_display 表的 id 操作）"""
        conn = db._get_conn()
        try:
            if tenant_id:
                row = conn.execute(
                    "SELECT id, aliases FROM member_display WHERE id = ? AND tenant_id = ?",
                    (alias_id, tenant_id),
                ).fetchone()
                if not row:
                    return {"ok": False, "error": "别名不存在或不属于当前租户"}
            conn.execute("DELETE FROM member_display WHERE id = ?", (alias_id,))
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────
    # 显示名管理
    # ──────────────────────────────────────────

    def get_display_names(self, tenant_id: str) -> list[dict]:
        """获取显示名列表"""
        conn = db._get_conn()
        rows = conn.execute(
            "SELECT md.*, COALESCE(g.name, '') AS group_name "
            "FROM member_display md "
            "LEFT JOIN member_groups g ON g.id = md.group_id AND g.tenant_id = md.tenant_id "
            "WHERE md.tenant_id = ? "
            "ORDER BY md.display_name",
            (tenant_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def add_display_name(
        self, raw_name: str, display_name: str, tenant_id: str,
        group_id: int | None = None, count_enabled: bool = True
    ) -> dict:
        """添加显示名记录"""
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        match_key = raw_name.strip().lower().replace(" ", "")
        try:
            conn.execute(
                "INSERT INTO member_display (raw_name, display_name, match_key, "
                "count_enabled, group_id, tenant_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (raw_name.strip(), display_name.strip(), match_key,
                 1 if count_enabled else 0, group_id, tenant_id, now, now),
            )
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def delete_display_name(self, item_id: int, tenant_id: str) -> dict:
        """删除显示名（带租户校验）"""
        conn = db._get_conn()
        try:
            row = conn.execute(
                "SELECT id FROM member_display WHERE id = ? AND tenant_id = ?",
                (item_id, tenant_id),
            ).fetchone()
            if not row:
                return {"ok": False, "error": "记录不存在或不属于当前租户"}
            conn.execute("DELETE FROM member_display WHERE id = ?", (item_id,))
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def assign_group(
        self, display_name: str, group_name: str, tenant_id: str
    ) -> dict:
        """为成员分配分组（通过分组名查询 group_id）"""
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        try:
            # 查找分组
            group = conn.execute(
                "SELECT id FROM member_groups WHERE name = ? AND tenant_id = ?",
                (group_name.strip(), tenant_id),
            ).fetchone()
            if not group:
                return {"ok": False, "error": f"分组 '{group_name}' 不存在"}
            group_id = group[0]

            # 更新 member_display
            conn.execute(
                "UPDATE member_display SET group_id = ?, updated_at = ? "
                "WHERE display_name = ? AND tenant_id = ?",
                (group_id, now, display_name.strip(), tenant_id),
            )
            conn.commit()
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ──────────────────────────────────────────
    # 发现未记录的成员
    # ──────────────────────────────────────────

    def discover_unknown(self, tenant_id: str) -> dict:
        """
        扫描 zoom_participants 找未在 member_display 中记录的成员名。
        返回 {members: [{name, count, first_seen, last_seen}]}
        """
        conn = db._get_conn()
        unknown = conn.execute(
            """
            SELECT zp.name, COUNT(*) AS cnt,
                   MIN(zp.action_time) AS first_seen,
                   MAX(zp.action_time) AS last_seen
            FROM zoom_participants zp
            LEFT JOIN member_display md
                ON REPLACE(LOWER(TRIM(zp.name)), ' ', '')
                   = REPLACE(LOWER(TRIM(md.match_key)), ' ', '')
                AND md.tenant_id = ?
            WHERE md.id IS NULL AND zp.tenant_id = ?
            GROUP BY zp.name
            ORDER BY cnt DESC
            LIMIT 100
            """,
            (tenant_id, tenant_id),
        ).fetchall()
        return {
            "members": [
                {"name": r[0], "count": r[1], "first_seen": r[2], "last_seen": r[3]}
                for r in unknown
            ]
        }
