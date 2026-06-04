# Time System Refactor — Phase 1 Findings

> 验证时间: 2026-06-04 17:58 MYT
> 状态: 仅分析，未改代码

---

## 核心发现

### 1. Dashboard 为空的根因 — API 路由不存在

**Dashboard 模板（`templates/dashboard.html`）第 17 行 JS 请求 `/api/v3/dashboard`，但这个路由在服务器和本地的 `app.py` 中均未定义。**

```javascript
var r = await fetch("/api/v3/dashboard?_=" + Date.now(), {cache:"no-store"});
var j = await r.json();
if(!j.ok) return;
document.getElementById("kpiParticipants").textContent = j.participant_count;
```

JS 请求返回 404（FastAPI 默认返回 `{"detail":"Not Found"}`），解析为 JSON 后 `j.ok` 为 undefined，触发 `if(!j.ok) return;` — 直接静默退出，所有 KPI 维持初始值 `—`。

**这不是时间问题，是路由缺失问题。**

| 检查项 | 结果 |
|--------|:----:|
| `/api/v3/dashboard` 路由存在？ | ❌ 不存在 |
| DB 有今天的数据？ | ✅ 8 条 (2026-06-04 UTC) |
| `/api/v2/live` 可用？ | ✅ 返回数据 |
| 共享 API 可用？ | ✅ `/api/v3/sharing-live` 返回正常 |

### 2. Participants 页面全是 `—` 的根因

服务器上的 `templates/participants.html` 是修改过的版本（来自未提交的服务器 diff），与本地不一致。需要检查参与者的实际 API 端点。

### 3. Sharing 统计

`/api/v3/sharing-live` 返回正常但 `current: 0`。因 zoom-monitor 已停，poll 不采集共享数据，共享仅依赖 webhook 事件。

### 4. 时间体系问题状态

虽然本次发现 Dashboard 空白的根因不是时间问题，但 `TIME_REFACTOR_PLAN.md` 中列出的以下问题仍存在且影响参会明细：

| 文件:行 | 问题 | 影响 |
|---------|------|------|
| `app.py:1454` | `is_late` 使用 UTC hour vs MYT 阈值 | 迟到/早退判断错误 |
| `monitor.py:86,123` | DB 存入 MYT 时间 | 跨天统计可能错误 |
| `app.py` 多行 | `today_str = datetime.now(timezone.utc).strftime` | MYT 日期边界与 UTC 日期不匹配 |

---

## 结论

> Dashboard 为空的原因不是时间体系，而是 **`/api/v3/dashboard` 路由缺失**（前端代码引用了不存在的 API）。

修复方案：

1. **创建 `/api/v3/dashboard` 路由**，聚合以下数据：
   - `participant_count`: 今日唯一参与者数（基于 `today_utc_range()`）
   - `online_count`: 当前在线（来自 Zoom Metrics API）
   - `join_count` / `leave_count`: 今日进出次数
   - `participants`: 参与者列表（含时长、在线状态）

2. 或修改前端 JS，改为调用已有的 `/api/v2/live` 或 `/api/v2/summary`

时间问题仍然存在（`monitor.py` 存 MYT、`is_late` 用 UTC），但不是 Dashboard 空白的根因。
