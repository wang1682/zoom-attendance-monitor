# Dashboard V3 API 修复报告

> **问题:** Dashboard 页面显示全部 `—`，因前端 JS 请求 `/api/v3/dashboard` 但后端未注册该路由
> **修复:** 新增 `GET /api/v3/dashboard` 路由，兼容前端 JS 字段要求

---

## 问题分析

### 前端代码 (templates/dashboard.html)

```javascript
var r = await fetch("/api/v3/dashboard?_=" + Date.now(), {cache:"no-store"});
var j = await r.json();
if(!j.ok) return;

document.getElementById("kpiParticipants").textContent = j.participant_count;
document.getElementById("kpiOnline").textContent = j.online_count;
document.getElementById("kpiJoin").textContent = j.join_count;
document.getElementById("kpiLeave").textContent = j.leave_count;
document.getElementById("kpiSharing").textContent = j.sharing_count;
```

JS 请求返回 404（路由不存在），`j.ok` 为 undefined，触发 `if(!j.ok) return;` — 静默退出，所有 KPI 维持 `—`。

### 修复内容

在 `/api/v3/live` 后新增路由 `GET /api/v3/dashboard`。

**返回字段:**

| 字段 | 说明 | 来源 |
|------|------|------|
| `ok` | 状态 | always true |
| `participant_count` | 今日唯一参与者数 | DB `COUNT(DISTINCT name)` |
| `online_count` | 当前在线 | Zoom Metrics API |
| `join_count` | 今日加入次数 | DB `COUNT(*) WHERE action='enter'` |
| `leave_count` | 今日离开次数 | DB `COUNT(*) WHERE action='leave'` |
| `participants` | 参与者列表 | DB 最新 50 条去重 |
| `sharing_count` | 当前共享数 | `/api/v3/sharing-live` |
| `meetings` | 会议列表 | Zoom Metrics API |

### 验证结果

```bash
curl -s http://127.0.0.1:8082/api/v3/dashboard

{
  "ok": true,
  "participant_count": 4,    # 今日 4 名参与者
  "online_count": 4,         # 当前 4 人在线
  "join_count": 0,           # 今日 0 次加入（UTC 边界问题，待时间重构修复）
  "leave_count": 8,          # 今日 8 次离开
  "sharing_count": 0,        # 当前无共享
  "participants": [...],     # 4 名参与者详情
  "meetings": [...]          # 当前会议
}
```

### Dashboard 页面恢复后预期

| KPI | 之前 | 现在 |
|-----|:----:|:----:|
| 今日活跃成员 | — | 4 |
| 当前会议中 | — | 4 |
| 今日加入次数 | — | 0* |
| 今日离开次数 | — | 8 |

*注: `join_count=0` 因 enter 事件发生在昨天的 UTC 时间，时间重构后将统一为 MYT 日期边界。
