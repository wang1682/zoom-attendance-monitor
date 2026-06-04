# Zoom Attendance Monitor — 时间目标状态

> 本文件定义项目的最终时间规范，所有模块必须遵守。
> 除非本文件被正式修订，任何新代码不得偏离此规范。

---

## 核心原则

```
存储层:    UTC ISO 8601 (带 +00:00)
计算层:    UTC (datetime.now(timezone.utc))
业务阈值:  配置值为 MYT，内部统一转 UTC 后比较
显示层:    Asia/Kuala_Lumpur (MYT, UTC+8)
```

---

## 具体规则

### 1. 数据库

| 列 | 格式 | 示例 |
|---|------|------|
| `action_time` | `YYYY-MM-DDTHH:MM:SS.ffffff+00:00` | `2026-06-05T03:15:22.123456+00:00` |
| `created_at` | 同上 | 同上 |
| `start_time` | 同上 | 同上 |
| `end_time` | 同上 | 同上 |
| `first_seen` | 同上 | 同上 |
| `last_seen` | 同上 | 同上 |

### 2. 后端 Python

| 场景 | 正确写法 |
|------|----------|
| 当前时间 | `datetime.now(timezone.utc)` |
| MYT 当前时间 | `datetime.now(timezone.utc).astimezone(MYT)` |
| MYT 今日起点 | `myt_now.replace(hour=0, minute=0, second=0, microsecond=0)` |
| MYT 今日→UTC | `myt_today_start.astimezone(timezone.utc).isoformat()` |
| 解析 ISO 字符串 | `datetime.fromisoformat(s.replace("Z", "+00:00"))` |

### 3. 工具函数

```python
MYT = timezone(timedelta(hours=8))

def utc_now() -> datetime:
    return datetime.now(timezone.utc)

def myt_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(MYT)

def myt_day_range_to_utc(dt: datetime = None) -> tuple[str, str]:
    """MYT 某日的 UTC 起止时间"""
    if dt is None:
        dt = myt_now()
    myt_start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    myt_end = myt_start + timedelta(days=1)
    return (myt_start.astimezone(timezone.utc).isoformat(),
            myt_end.astimezone(timezone.utc).isoformat())
```

### 4. SQL 查询

```sql
-- ✅ 正确：使用 UTC 边界查询
WHERE action_time >= ? AND action_time < ?

-- ❌ 错误：使用日期字符串前缀匹配
WHERE action_time >= '2026-06-04'
```

### 5. 前端显示

```javascript
// base.html 中的 to_myt() 函数 — 使用 timeZone: 'Asia/Kuala_Lumpur'
function to_myt(s) {
    if (!s || s === '—') return '—';
    const d = new Date(s.replace('Z', '+00:00'));
    return d.toLocaleString('zh-CN', {timeZone: 'Asia/Kuala_Lumpur'});
}
```

### 6. Jinja2 Filter

```python
@app.template_filter("myt")
def myt_filter(iso_str: str, fmt: str = "%m-%d %H:%M:%S") -> str:
    if not iso_str or iso_str == "—":
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return dt.astimezone(MYT).strftime(fmt)
    except:
        return iso_str
```

### 7. API 返回

API 返回的 ISO 时间字符串统一为 UTC（带 `+00:00`），由前端 JS 的 `to_myt()` 转 MYT。

```json
{
  "action_time": "2026-06-05T03:15:22.123456+00:00"
}
```

---

## 禁止的做法

| ❌ 禁止 | 原因 |
|---------|------|
| `datetime.now()`（无时区） | 产生 naive datetime |
| `datetime.now(timezone.utc).strftime("%Y-%m-%d")` 做日期过滤 | 字符串前缀比较，跨 UTC 日断裂 |
| 在 SQL 中使用 `datetime('now')` | SQLite 本地时间，时区不明确 |
| 后端返回 MYT 时间字符串 | 前端无法统一展示 |
| 变量名含 `now_myt` 但实际是 UTC | 误导维护 |
| `fromisoformat(s)` 不加 `replace("Z")` | Z 后缀导致 parse 失败 |
