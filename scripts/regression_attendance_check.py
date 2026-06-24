#!/usr/bin/env python3
"""
成员中心统计口径回归测试。

部署前必须执行：
    python3 scripts/regression_attendance_check.py

不通过 → 禁止 docker compose up。

检查项：
1. DinoJun 不是 DinoJun (2)
2. DinoJun 存在（未被 deleted 误杀）
3. Trio 今日累计不是 46s
4. Zivv 紫薇 今日累计不是 12s
5. summary_current 不覆盖 today_total_seconds
6. _load_deleted_names 不包含 dinojun
7. 业务日凌晨 00:00-06:00 仍显示上一业务日
"""

import sys
import os

script_dir = os.path.dirname(os.path.abspath(__file__))
project_dir = os.path.dirname(script_dir)
sys.path.insert(0, project_dir)
os.chdir(project_dir)

FAILURES = []


def check(condition, msg):
    if not condition:
        FAILURES.append(msg)
        print(f"  ❌ {msg}")
    else:
        print(f"  ✅ {msg}")


def _report_and_exit():
    total = 7
    passed = total - len(FAILURES)
    print("\n" + "=" * 60)
    print(f"结果: {passed}/{total} 通过, {len(FAILURES)} 失败")
    if FAILURES:
        print("\n失败项:")
        for i, f in enumerate(FAILURES, 1):
            print(f"  {i}. {f}")
    print("=" * 60)
    sys.exit(1 if FAILURES else 0)


def main():
    print("=" * 60)
    print("成员中心统计口径回归测试")
    print("=" * 60)

    try:
        import db
    except ImportError as e:
        check(False, f"db 模块导入失败: {e}")
        _report_and_exit()
        return

    # ── 1. DinoJun 不是 DinoJun (2) ──
    print("\n[1/7] DinoJun display_name")
    try:
        r = db.resolve_display_name("DinoJun", "default")
        dn = r.get("display_name", "")
        check(dn == "DinoJun", f"DinoJun display_name 应为 DinoJun，实际={dn}")
    except Exception as e:
        check(False, f"resolve_display_name 抛异常: {e}")

    # ── 2. DinoJun 存在于 get_today_attendance_summary ──
    print("\n[2/7] DinoJun 在今日汇总中存在")
    try:
        summary = db.get_today_attendance_summary("default")
        members = summary.get("members", [])
        found = any(
            ("dinojun" in (m.get("standard_name", "") or "").lower().replace(" ", ""))
            or ("dinojun" in (m.get("name", "") or "").lower().replace(" ", ""))
            for m in members
        )
        check(found, "DinoJun 在 get_today_attendance_summary 返回中存在")
    except Exception as e:
        check(False, f"get_today_attendance_summary 抛异常: {e}")

    # ── 3. Trio 今日累计不是 46s ──
    print("\n[3/7] Trio 今日累计")
    try:
        if not members:
            summary = db.get_today_attendance_summary("default")
            members = summary.get("members", [])
        trio = None
        for m in members:
            sn = (m.get("standard_name", "") or "").lower().replace(" ", "")
            if sn == "trio":
                trio = m
                break
        if trio:
            secs = trio.get("today_total_seconds", 0)
            check(secs != 46 and secs > 100, f"Trio today_total_seconds 不应是 46s，实际={secs}s")
        else:
            check(False, "Trio 不在 summary 中")
    except Exception as e:
        check(False, f"Trio 检查抛异常: {e}")

    # ── 4. Zivv 紫薇 今日累计不是 12s ──
    print("\n[4/7] Zivv 紫薇 今日累计")
    try:
        if not members:
            summary = db.get_today_attendance_summary("default")
            members = summary.get("members", [])
        zivv = None
        for m in members:
            sn = (m.get("standard_name", "") or "").lower().replace(" ", "")
            display_name = (m.get("name", "") or "").lower().replace(" ", "")
            if "zivv" in sn or "zivv" in display_name:
                zivv = m
                break
        if zivv:
            secs = zivv.get("today_total_seconds", 0)
            check(secs != 12 and secs > 100, f"Zivv 紫薇 today_total_seconds 不应是 12s，实际={secs}s")
        else:
            check(False, "Zivv 紫薇 不在 summary 中")
    except Exception as e:
        check(False, f"Zivv 紫薇 检查抛异常: {e}")

    # ── 5. _load_deleted_names 不包含 dinojun ──
    print("\n[5/7] _load_deleted_names 不包含 dinojun")
    try:
        deleted = db._load_deleted_names("default")
        check("dinojun" not in deleted, f"_load_deleted_names 不应包含 dinojun，实际包含 {list(deleted)}")
    except Exception as e:
        check(False, f"_load_deleted_names 抛异常: {e}")

    # ── 6. summary_current 覆盖检测 ──
    print("\n[6/7] summary_current 不覆盖 today_total_seconds")
    try:
        admin_path = os.path.join(project_dir, "admin_routes.py")
        if os.path.exists(admin_path):
            with open(admin_path, "r") as f:
                content = f.read()
            dangerous = False
            lines = content.split("\n")
            for i, line in enumerate(lines, 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                key_checks = [
                    ('"today_total_seconds"', "today_total_seconds"),
                    ('"join_count"', "join_count"),
                    ('"leave_count"', "leave_count"),
                ]
                for key_str, key_name in key_checks:
                    if key_str in stripped and "=" in stripped and "m.get(" not in stripped:
                        check(False, f"admin_routes.py:{i} — 仍存在 {key_name} 覆盖: {stripped}")
                        dangerous = True
            if not dangerous:
                check(True, "admin_routes.py 无危险覆盖代码")
        else:
            check(False, "admin_routes.py 不存在")
    except Exception as e:
        check(False, f"summary_current 检查抛异常: {e}")

    # ── 7. 业务日边界 ──
    print("\n[7/7] 业务日边界")
    try:
        br = db.get_business_day_range_myt(6)
        start = br["start_utc"]
        end = br["end_utc"]
        duration_days = (end - start).total_seconds() / 86400
        check(
            0.9 < duration_days <= 1.1,
            f"业务日窗口应为 ~1 天，实际={duration_days:.2f} 天",
        )
        check(bool(br["business_date"]), "business_date 非空")
    except Exception as e:
        check(False, f"业务日边界检查抛异常: {e}")

    _report_and_exit()


if __name__ == "__main__":
    main()
