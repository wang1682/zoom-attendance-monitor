import re

with open("/opt/zoom-attendance-monitor/db.py") as f:
    content = f.read()

# Find the name_map_cache building section
old = '''    for mr in md_rows:
        mrd = dict(mr)
        t_id = mrd.get("tenant_id") or tenant_id
        for alias in [mrd.get("raw_name", ""), mrd.get("display_name", "")]:
            key = alias.strip().lower().replace(" ", "")
            if key:
                name_map_cache[(t_id, key)] = mrd.get("display_name") or mrd.get("raw_name", "")
        for alias in json.loads(mrd.get("aliases") or "[]"):
            key = alias.strip().lower().replace(" ", "")
            if key:
                name_map_cache[(t_id, key)] = mrd.get("display_name") or mrd.get("raw_name", "")'''

new = '''    for mr in md_rows:
        mrd = dict(mr)
        t_id = mrd.get("tenant_id") or tenant_id
        disp = mrd.get("display_name") or mrd.get("raw_name", "")
        # 跳过 (2) 变体记录 - 如果该 key 已被主记录占用则跳过
        is_variant = disp.endswith(" (2)")
        for alias in [mrd.get("raw_name", ""), disp]:
            key = alias.strip().lower().replace(" ", "")
            if not key:
                continue
            existing = name_map_cache.get((t_id, key))
            if existing and is_variant:
                continue  # 主记录优先，跳过变体
            if is_variant and not existing:
                continue  # 没有主记录也不写变体
            name_map_cache[(t_id, key)] = disp
        for alias in json.loads(mrd.get("aliases") or "[]"):
            key = alias.strip().lower().replace(" ", "")
            if not key:
                continue
            existing = name_map_cache.get((t_id, key))
            if existing and is_variant:
                continue
            if is_variant and not existing:
                continue
            name_map_cache[(t_id, key)] = disp'''

if old in content:
    content = content.replace(old, new)
    with open("/opt/zoom-attendance-monitor/db.py", "w") as f:
        f.write(content)
    print("REPLACED OK")
else:
    print("WARNING: old string not found!")
    idx = content.find("for mr in md_rows:")
    if idx >= 0:
        print(content[idx:idx+800])
