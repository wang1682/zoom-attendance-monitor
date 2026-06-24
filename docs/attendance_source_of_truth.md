# 成员中心统计口径 — 技术冻结文档

> 生效版本：stable-v0.2.3
> 生效日期：2026-06-25
> 后续任何改动必须先跑 `scripts/regression_attendance_check.py`，不通过禁止部署。

## 一、用户名标准化

**唯一来源**：`member_display` 表（`deleted=0`）

**解析顺序**（`resolve_display_name`）：

1. 优先匹配 `match_key`（归一化后的 key）
2. 次优先匹配 `raw_name` 精确命中
3. `aliases` 字段也参与匹配
4. 重复 `raw_name` 的记录中，`aliases` 主记录（包含完整 aliases 列表）优先于无 aliases 的重复记录

**坑点**：

- 两条记录 `raw_name="DinoJun"` 时，不应让 `display_name="DinoJun (2)"` 覆盖 `display_name="DinoJun"`
- `member_display` 的 `(2)` 变体不能覆盖主账号的 display_name
- `name_map_cache` 构建时：若已有非 `(2)` 变体映射到同一 key，则跳过 `(2)` 变体

## 二、软删除逻辑

**`_load_deleted_names`**：

- 只读取 `deleted=1` 的记录
- 对每条 `deleted=1` 记录的 `raw_name` 和 `aliases` 做归一化（lower + 去空格）
- **关键保护**：加入 deleted set 前，检查是否有 `deleted=0` 的记录已占用该 key
- 若 active 记录已占用，则该 key 不加入 deleted set

## 三、今日累计

| 字段 | 来源 | 说明 |
|------|------|------|
| `today_total_seconds` | `participant_sessions` | session 累计时长 |
| `session_count` | `zoom_participants` | 业务日内 enter/joined 次数 |
| `disconnect_count` | `zoom_participants` | 业务日内 leave/left 次数 |
| `join_count` | `zoom_participants` | enter/joined 事件数 |
| `leave_count` | `zoom_participants` | leave/left 事件数 |
| `is_online` | `participant_sessions` | open_session_started_at 是否有效 |

**`participant_sessions` 只负责时长，不负责次数。**

**`zoom_participants` 统计条件**：

- `action IN ('enter','joined')` → join_count
- `action IN ('leave','left')` → leave_count
- `waiting_room_enter` / `admitted` / `breakout_enter` / `breakout_leave` 不计入
- 统计前用 `name_map_cache` 把 raw_name 合并为 `standard_name`（Dino Jun + DinoJun → DinoJun）

## 四、业务日边界

- 分割时间：MYT **06:00**（UTC 前一天的 22:00）
- 凌晨 00:00-05:59 MYT 属于上一业务日
- `get_business_day_range_myt(hour=6)` 为标准入口

## 五、当前会议（summary_current）的覆盖规则

**summary_current 只能覆盖以下实时字段**：

- `first_join`
- `last_activity`
- `last_leave_time`

**保留不变（不可覆盖）**：

- `today_total_seconds`
- `today_total_duration`
- `session_count`
- `disconnect_count`
- `join_count`
- `leave_count`

一句话：当前会议片段不能覆盖完整业务日累计。

## 六、关键函数清单

| 函数 | 位置 | 职责 |
|------|------|------|
| `get_today_attendance_summary` | `db.py` | 入口，session 优先 fallback 旧算法 |
| `get_today_from_sessions` | `db.py` | 读 participant_sessions 按业务日聚合 |
| `_build_session_summary` | `db.py` | session_rows → 格式化输出（含 zoom_participants 统计） |
| `_load_deleted_names` | `db.py` | 加载软删除列表（已修复 active key 保护） |
| `resolve_display_name` | `db.py` | 用户名标准化 |
| `get_business_day_range_myt` | `db.py` | 业务日边界计算 |

## 七、回归测试

部署前必须执行：

```bash
python3 scripts/regression_attendance_check.py
```

不通过 → 禁止 `docker compose up`。

## 八、变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-06-25 | stable-v0.2.3 | 首次冻结：summary_current 不再覆盖今日累计，join/leave 改用 zoom_participants 统计 |
