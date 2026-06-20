"""
GroupService — 统一分组管理

所有分组相关操作的统一入口。
核心原则：group 按 tenant_id 隔离，所有操作必须带租户参数。

依赖：db.py（底层 SQLite）
"""

import json
from datetime import datetime, timezone
from typing import Any

import db


class GroupService:
    """分组管理服务"""

    # ──────────────────────────────────────────
    # 默认分组
    # ──────────────────────────────────────────

    @staticmethod
    def default_groups() -> list[dict]:
        """每个租户的标准默认分组列表"""
        return [
            {"name": "group1", "description": "Group 1"},
            {"name": "group2", "description": "Group 2"},
            {"name": "group3", "description": "Group 3"},
            {"name": "group4", "description": "Group 4"},
            {"name": "group5", "description": "Group 5"},
            {"name": "group6", "description": "Group 6"},
            {"name": "group7", "description": "Group 7"},
            {"name": "group8", "description": "Group 8"},
            {"name": "group9", "description": "Group 9"},
            {"name": "group10", "description": "Group 10"},
            {"name": "AI", "description": "AI / ML team"},
            {"name": "QA", "description": "Quality Assurance"},
            {"name": "IT", "description": "Information Technology"},
            {"name": "EM", "description": "Executive Management"},
        ]

    # ──────────────────────────────────────────
    # seed
    # ──────────────────────────────────────────

    def seed_groups(self, tenant_id: str) -> int:
        """
        为本租户创建默认分组（幂等，已有则跳过）。
        返回新创建的分组数。
        """
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        count = 0
        for g in self.default_groups():
            existing = conn.execute(
                "SELECT id FROM member_groups WHERE name = ? AND tenant_id = ?",
                (g["name"], tenant_id),
            ).fetchone()
            if existing:
                continue
            conn.execute(
                "INSERT INTO member_groups (name, description, tenant_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (g["name"], g["description"], tenant_id, now, now),
            )
            count += 1
        conn.commit()
        return count

    # ──────────────────────────────────────────
    # 查询分组
    # ──────────────────────────────────────────

    def get_all_groups(self, tenant_id: str) -> list[dict]:
        """
        获取本租户所有分组（含成员列表）。
        """
        conn = db._get_conn()
        groups = conn.execute(
            "SELECT * FROM member_groups WHERE tenant_id = ? ORDER BY name",
            (tenant_id,),
        ).fetchall()
        result = []
        for g in groups:
            gd = dict(g)
            # 从 member_display 读该分组成员
            members = conn.execute(
                "SELECT display_name FROM member_display "
                "WHERE group_id = ? AND tenant_id = ? ORDER BY display_name",
                (gd["id"], tenant_id),
            ).fetchall()
            if not members:
                # 回退旧表
                members = conn.execute(
                    "SELECT member_name FROM member_group_members "
                    "WHERE group_id = ? ORDER BY member_name",
                    (gd["id"],),
                ).fetchall()
            gd["members"] = [m[0] for m in members]
            gd["member_count"] = len(gd["members"])
            result.append(gd)
        return result

    def get_group(self, group_id: int, tenant_id: str) -> dict | None:
        """获取单个分组（带租户校验）"""
        conn = db._get_conn()
        row = conn.execute(
            "SELECT * FROM member_groups WHERE id = ? AND tenant_id = ?",
            (group_id, tenant_id),
        ).fetchone()
        if not row:
            return None
        return dict(row)

    def get_member_group(self, member_name: str, tenant_id: str) -> str | None:
        """获取某成员所在的分组名"""
        if not member_name:
            return None
        conn = db._get_conn()
        name = member_name.strip().lower().replace(" ", "")

        # 1. 优先从 member_display.group_id 读取
        row = conn.execute(
            "SELECT g.name FROM member_groups g "
            "JOIN member_display md ON md.group_id = g.id "
            "WHERE (REPLACE(LOWER(TRIM(md.raw_name)), ' ', '') = ? "
            "   OR REPLACE(LOWER(TRIM(md.display_name)), ' ', '') = ?) "
            "AND md.group_id IS NOT NULL AND md.tenant_id = ? AND g.tenant_id = ?",
            (name, name, tenant_id, tenant_id),
        ).fetchone()
        if row:
            return row[0]

        # 2. 回退旧 member_group_members 表
        row = conn.execute(
            "SELECT g.name FROM member_groups g "
            "JOIN member_group_members m ON m.group_id = g.id "
            "WHERE REPLACE(LOWER(TRIM(m.member_name)), ' ', '') = ? "
            "AND g.tenant_id = ?",
            (name, tenant_id),
        ).fetchone()
        return row[0] if row else None

    # ──────────────────────────────────────────
    # 添加成员到分组
    # ──────────────────────────────────────────

    def add_member(self, group_id: int, member_name: str, tenant_id: str) -> bool:
        """
        添加成员到分组。
        校验：group 必须属于本租户。
        """
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        try:
            # 校验 group 租户归属
            group = conn.execute(
                "SELECT tenant_id FROM member_groups WHERE id = ?",
                (group_id,),
            ).fetchone()
            if not group:
                db.log_audit("reject", "member_group_member", group_id,
                             f"分组不存在: group_id={group_id}")
                return False
            if group[0] != tenant_id:
                db.log_audit("reject", "member_group_member", group_id,
                             f"跨租户拒绝: group租户{group[0]}!=成员租户{tenant_id}, member={member_name}")
                return False

            # 新方式：写 member_display.group_id
            existing = conn.execute(
                "SELECT id FROM member_display WHERE raw_name = ? AND tenant_id = ?",
                (member_name.strip(), tenant_id),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE member_display SET group_id = ?, updated_at = ? WHERE id = ?",
                    (group_id, now, existing[0]),
                )
            else:
                match_key = member_name.strip().lower().replace(" ", "")
                existing_by_key = conn.execute(
                    "SELECT id, raw_name FROM member_display "
                    "WHERE match_key = ? AND raw_name != ? AND tenant_id = ?",
                    (match_key, member_name.strip(), tenant_id),
                ).fetchone()
                if existing_by_key:
                    conn.execute(
                        "UPDATE member_display SET raw_name = ?, display_name = ?, "
                        "group_id = ?, updated_at = ? WHERE id = ?",
                        (member_name.strip(), existing_by_key[1], group_id, now, existing_by_key[0]),
                    )
                else:
                    conn.execute(
                        "INSERT INTO member_display (raw_name, display_name, match_key, "
                        "group_id, tenant_id, created_at, updated_at) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (member_name.strip(), member_name.strip(), match_key, group_id, tenant_id, now, now),
                    )

            # 旧方式：同时写 member_group_members 保持兼容
            conn.execute(
                "DELETE FROM member_group_members WHERE member_name = ? AND group_id != ?",
                (member_name.strip(), group_id),
            )
            conn.execute(
                "INSERT OR IGNORE INTO member_group_members (group_id, member_name, created_at) "
                "VALUES (?, ?, ?)",
                (group_id, member_name.strip(), now),
            )
            conn.commit()
            db.log_audit("create", "member_group_member", group_id,
                         f"Added member {member_name} to group {group_id} (tenant={tenant_id})")
            return True
        except Exception:
            return False

    # ──────────────────────────────────────────
    # 移除成员从分组
    # ──────────────────────────────────────────

    def remove_member(self, group_id: int, member_name: str, tenant_id: str) -> bool:
        """移除成员（带租户校验）"""
        conn = db._get_conn()
        try:
            group = conn.execute(
                "SELECT tenant_id FROM member_groups WHERE id = ?",
                (group_id,),
            ).fetchone()
            if not group or group[0] != tenant_id:
                return False
            conn.execute(
                "DELETE FROM member_group_members WHERE group_id = ? AND member_name = ?",
                (group_id, member_name.strip()),
            )
            conn.execute(
                "UPDATE member_display SET group_id = NULL, updated_at = ? "
                "WHERE raw_name = ? AND group_id = ? AND tenant_id = ?",
                (datetime.now(timezone.utc).isoformat(), member_name.strip(), group_id, tenant_id),
            )
            conn.commit()
            return True
        except Exception:
            return False

    # ──────────────────────────────────────────
    # 删除分组
    # ──────────────────────────────────────────

    def delete_group(self, group_id: int, tenant_id: str) -> bool:
        """删除分组（带租户校验）"""
        conn = db._get_conn()
        try:
            group = conn.execute(
                "SELECT tenant_id FROM member_groups WHERE id = ?",
                (group_id,),
            ).fetchone()
            if not group or group[0] != tenant_id:
                return False
            conn.execute("UPDATE member_display SET group_id = NULL, updated_at = ? "
                         "WHERE group_id = ? AND tenant_id = ?",
                         (datetime.now(timezone.utc).isoformat(), group_id, tenant_id))
            conn.execute("DELETE FROM member_group_members WHERE group_id = ?", (group_id,))
            conn.execute("DELETE FROM member_groups WHERE id = ?", (group_id,))
            conn.commit()
            db.log_audit("delete", "member_group", group_id, f"Deleted group (tenant={tenant_id})")
            return True
        except Exception:
            return False

    # ──────────────────────────────────────────
    # 更新分组
    # ──────────────────────────────────────────

    def create_group(self, name: str, description: str, tenant_id: str) -> int | None:
        """创建分组（自动归属本租户）"""
        try:
            conn = db._get_conn()
            now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT INTO member_groups (name, description, tenant_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (name.strip(), description.strip(), tenant_id, now, now),
            )
            conn.commit()
            return cur.lastrowid
        except Exception:
            return None

    def update_group(self, group_id: int, name: str, description: str, tenant_id: str) -> bool:
        """更新分组（带租户校验）"""
        conn = db._get_conn()
        now = datetime.now(timezone.utc).isoformat()
        try:
            group = conn.execute(
                "SELECT tenant_id FROM member_groups WHERE id = ?",
                (group_id,),
            ).fetchone()
            if not group or group[0] != tenant_id:
                return False
            conn.execute(
                "UPDATE member_groups SET name = ?, description = ?, updated_at = ? WHERE id = ?",
                (name, description, now, group_id),
            )
            conn.commit()
            return True
        except Exception:
            return False
