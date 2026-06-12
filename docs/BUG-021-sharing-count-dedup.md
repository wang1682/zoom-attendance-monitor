# BUG-021: Dashboard sharing_count 跨会议重复计数

**Severity:** Low
**Impact:** Statistics only
**Data loss:** No
**Customer visible:** Minor（列表正常，头部计数虚高）

## 现象

Dashboard 显示 "当前共享：2人"，但实际只有 1 人（ovilia--99）在共享。

## 根因

C（主因：Dashboard 统计错误）+ B（诱因：Zoom 跨会议重复）

**代码路径：**

1. `zoom_metrics.py:190-195` — `online_list` 构建时不做跨会议去重
   - 对比 `total_online` 使用 `all_canonical` 集合跨会议去重（line 182-186），在线人数正确
   - 但 `online_list` 只是拼接所有会议的 `participants`，同一个人在 N 个会议 → 出现 N 次

2. `app.py:2325` — `sharing_count = len([p for p in live_data.get("online_list", []) if p.get("is_sharing")])`
   - 直接 `len()`，无去重

3. `sharing_live` 表为空 — 数据全部来自 Zoom Metrics API，与 sharing_live 无关

## 修复方案（已确定，不现在修）

**单点修复，仅改 app.py:2325**

```python
# Before (无去重):
sharing_count = len([p for p in live_data.get("online_list", []) if p.get("is_sharing")])

# After (按姓名去重):
seen_sharing = set()
for p in live_data.get("online_list", []):
    if p.get("is_sharing"):
        seen_sharing.add(p.get("name", ""))
sharing_count = len(seen_sharing)
```

**不改 `zoom_metrics.py` 的 `online_list`**，因为 `online_list` 被 Dashboard / Live / Attendance / Analytics 多个模块依赖。

## 阈值升级条件

如果 `sharing_count` 显著大于 `total_online`（如 11 vs 24），升级为 P1。

## 触发条件

同一个人同时在多个 Zoom 会议中活跃且正在共享。

## 状态

Backlog
