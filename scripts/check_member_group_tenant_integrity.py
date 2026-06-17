#!/usr/bin/env python3
"""
跨租户 group_id 完整性自检脚本。

检测 member_display 表中 group_id 指向的 member_groups 租户，
是否与 member_display 自身所在租户一致。

报告格式：
  - 总脏数据数（exit code 2 表示有脏数据）
  - 按 member_tenant / group_tenant / group_name 聚合统计
  - 示例前 20 条记录

使用方式（容器内）：
  docker compose exec -T zoom-api python3 /app/scripts/check_member_group_tenant_integrity.py

使用方式（Docker cp 到容器后）：
  docker exec zoom-api python3 /app/scripts/check_member_group_tenant_integrity.py

返回码：
  0 — 无脏数据
  2 — 发现脏数据
"""

import sys
import json

try:
    import db
except ImportError:
    # 当外面直接 python3 scripts/xxx.py 时，先设 sys.path
    sys.path.insert(0, "/app")
    import db

conn = db._get_conn()

rows = conn.execute(
    """
    SELECT
      md.raw_name,
      md.display_name,
      md.tenant_id AS member_tenant,
      md.group_id,
      mg.name AS group_name,
      mg.tenant_id AS group_tenant
    FROM member_display md
    LEFT JOIN member_groups mg ON md.group_id = mg.id
    WHERE md.group_id IS NOT NULL
      AND mg.tenant_id != md.tenant_id
    ORDER BY md.tenant_id, mg.tenant_id, mg.name, md.raw_name
    """
).fetchall()

total = len(rows)
print(f"= 跨租户 group_id 自检报告 =\n")
print(f"检查时间: {__import__('datetime').datetime.now()}")
print(f"脏数据总数: {total}")
print()

if total == 0:
    print("✅ 无脏数据，全部正常。")
    sys.exit(0)

# ── 聚合统计 ──
from collections import Counter

agg = Counter()
for r in rows:
    key = (r["member_tenant"], r["group_tenant"], r["group_name"])
    agg[key] += 1

print("━" * 60)
print("按 member_tenant / group_tenant / group_name 聚合:")
print("━" * 60)
for (mt, gt, gn), cnt in sorted(agg.items()):
    print(f"  {cnt:>4} 条  |  {mt} → {gt}/{gn}")

print()
print("━" * 60)
print(f"示例前 20 条:")
print("━" * 60)
fmt = "{:<24} {:<16} {:<16} {:<6} → {:<16} {}"
print(fmt.format("raw_name", "member_tenant", "group_tenant", "gid", "group_name", "display_name"))
print("─" * 90)
for r in rows[:20]:
    print(
        fmt.format(
            r["raw_name"],
            r["member_tenant"],
            r["group_tenant"],
            str(r["group_id"]),
            r["group_name"],
            r["display_name"],
        )
    )
if total > 20:
    print(f"  ... 及 {total - 20} 条更多记录")
    print()

print(f"总计: {total} 条脏数据. 建议处理。")

sys.exit(2)
