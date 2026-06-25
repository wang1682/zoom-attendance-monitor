OLD_STRING = '''def _load_deleted_names(tenant_id: str | None = None) -> set:
    """加载被软删除的成员 user_key 集合。"""
    conn = _get_conn()
    if tenant_id:
        rows = conn.execute(
            "SELECT raw_name, display_name, aliases FROM member_display WHERE tenant_id=? AND deleted=1",
            (tenant_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT raw_name, display_name, aliases FROM member_display WHERE deleted=1"
        ).fetchall()
    deleted = set()
    for dr in rows:
        for n in (dr["raw_name"], dr["display_name"]):
            if n:
                deleted.add(n.strip().lower().replace(" ", ""))
        for alias in json.loads(dr["aliases"] or "[]"):
            if alias:
                deleted.add(alias.strip().lower().replace(" ", ""))
    return deleted'''

NEW_STRING = '''def _load_deleted_names(tenant_id: str | None = None) -> set:
    """加载被软删除的成员 user_key 集合。

    注意：如果一个 deleted 记录的 raw_name / alias 已经被 active（deleted=0）记录
    的 match_key、display_name 或 aliases 占用，则不加入删除名单。
    """
    conn = _get_conn()

    # 先加载所有 active 记录的 match_key + display_name + aliases
    if tenant_id:
        active_rows = conn.execute(
            "SELECT match_key, display_name, aliases FROM member_display WHERE tenant_id=? AND deleted=0",
            (tenant_id,),
        ).fetchall()
        rows = conn.execute(
            "SELECT raw_name, display_name, aliases FROM member_display WHERE tenant_id=? AND deleted=1",
            (tenant_id,),
        ).fetchall()
    else:
        active_rows = conn.execute(
            "SELECT match_key, display_name, aliases FROM member_display WHERE deleted=0"
        ).fetchall()
        rows = conn.execute(
            "SELECT raw_name, display_name, aliases FROM member_display WHERE deleted=1"
        ).fetchall()

    # 构建 active 占用的所有 key 集合
    active_keys = set()
    for ar in active_rows:
        mk = ar["match_key"]
        if mk:
            active_keys.add(mk.strip().lower().replace(" ", ""))
        dn = ar["display_name"]
        if dn:
            active_keys.add(dn.strip().lower().replace(" ", ""))
        for alias in json.loads(ar["aliases"] or "[]"):
            if alias:
                active_keys.add(alias.strip().lower().replace(" ", ""))

    # 构建 deleted 集合，排除已被 active 占用的 key
    deleted = set()
    for dr in rows:
        for n in (dr["raw_name"], dr["display_name"]):
            if n:
                key = n.strip().lower().replace(" ", "")
                if key not in active_keys:
                    deleted.add(key)
        for alias in json.loads(dr["aliases"] or "[]"):
            if alias:
                key = alias.strip().lower().replace(" ", "")
                if key not in active_keys:
                    deleted.add(key)
    return deleted'''

# Verify they match the file
with open("/opt/zoom-attendance-monitor/db.py") as f:
    content = f.read()

if OLD_STRING in content:
    print("FOUND old string in db.py - OK")
else:
    print("ERROR: old string NOT FOUND in db.py")
    # find what differs
    idx = content.find("def _load_deleted_names")
    if idx >= 0:
        print(content[idx:idx+600])
