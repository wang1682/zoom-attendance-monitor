# /settings/system 500 分析报告

## 结论

**导致 500 的直接原因：** Jinja2 模板引用了不存在的变量 `zoom_status`。

## 详细排查过程

### 触发路径

```
浏览器 → /settings/system
    → app.py 第 382 行: async def settings_system_page(request: Request)
    → return tmpl.TemplateResponse(request, "settings_system.html", {...})
    → templates/settings_system.html 第 48 行
    → {% if zoom_status.webhook_delay_text == '刚刚' %}
    → jinja2.exceptions.UndefinedError: 'zoom_status' is undefined
    → HTTP 500 Internal Server Error
```

### 异常信息

```
File "/app/templates/settings_system.html", line 48, in block 'content'
    {% if zoom_status.webhook_delay_text == '刚刚' %}st-ok
    ^^^^^^^^^^^^^^^^^^^^^^^^^
jinja2.exceptions.UndefinedError: 'zoom_status' is undefined
```

### 根因分析

**服务器上 settings_system.html（来自之前未提交的修改）** 新增了 `zoom_status` 相关的显示内容：

- 第 48-52 行: webhook 延迟状态显示
- 第 58-60 行: 会议数/在线数/共享数

**但路由 `settings_system_page`（app.py 第 382-396 行）** 没有传 `zoom_status` 变量，只传了：

```python
{
    "brand": BRAND,
    "version": "0.2.1",
    "docker_status": docker_status,
    "participant_count": ...,
}
```

缺少：
- `zoom_status` (包含 `webhook_delay_text`, `meeting_count`, `online_count`, `sharing_count`)

### 修复方案

在 `settings_system_page` 的返回上下文中添加 `zoom_status`：

```python
# 在 docker_status 之后，添加:
from zoom_metrics import ZoomMetrics
zm = ZoomMetrics()
import asyncio
loop = asyncio.new_event_loop()
asyncio.set_event_loop(loop)
live_data = loop.run_until_complete(zm.get_live())
loop.close()

zoom_status = {
    "webhook_delay_text": "刚刚",
    "meeting_count": len(live_data.get("meetings", [])),
    "online_count": live_data.get("total_online", 0),
    "sharing_count": live_data.get("sharing_count", 0),
}
```

然后加到 TemplateResponse 的 context 中。
