# Participants Pairing Fix Report

> **修复日期:** 2026-06-04 22:17 MYT
> **问题:** `/api/v2/summary` 的 enter/leave 配对因 UTC 日期边界断裂

---

## 根因

DB 数据:
```
Dino Jun   enter    2026-06-03T06:59:59Z  (MYT 06-03 14:59)
Dino Jun   leave    2026-06-04T17:25:53Z  (MYT 06-05 01:25)
```

原代码:
```python
today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")  # "2026-06-04"
rows = conn.execute("WHERE action_time >= ?", (today_str,))  # 只查到 leave, 查不到 enter
```

enter 和 leave 跨了 UTC 日期边界，但代码按 `today_str` 过滤把所有 enter 排除在外。

## 修复方案

分层双窗口设计：

| 窗口 | 范围 | 用途 |
|------|------|------|
| **配对窗口** | MYT 今日 -7 天 | 确保所有 enter 能被查到 |
| **统计窗口** | MYT 今日 00:00 ~ 明日 00:00 | 只输出此窗口内有活动的人 |

关键逻辑：
```python
# 配对: 查 7 天数据
rows = conn.execute("WHERE action_time >= ? AND action_time < ?",
    (lookup_start, report_end_utc))

# 过滤: 只输出今日窗口内有活动的人
has_today_activity = any(in_today_range(t) for t in enters + leaves)
if not has_today_activity and not has_online_now:
    continue
```

## 验证结果

```bash
curl -s http://127.0.0.1:8082/api/v2/summary

"enter_time": "2026-06-03T06:59:59+00:00",  # ✅ 不再为空
"leave_time": "2026-06-04T17:25:53+00:00",  # ✅ 正常
"duration_min": 3429,                       # ⚠️ 偏大（7天窗口拉入旧数据，待重构优化）
```

## 剩余问题

1. **duration_min 偏大** — 7天配对窗口导致旧 enter 与 now 配对，计算了从 06-02 到现在的累计时长。需要在时间重构中统一为"今日内实际在线时长"
2. **Alias 重复** — Dino Jun / DinoJun 仍然分开统计
3. **is_late/is_early_leave** — 仍使用 `enters[0].hour >= 9` 比较 UTC hour
