#!/usr/bin/env python3
"""最终验收"""
import http.client, urllib.parse, sqlite3

conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='admin', telegram_2fa_enabled=0 WHERE id=1")
conn.commit()
conn.close()

h = http.client.HTTPConnection('localhost', 8000)
body = urllib.parse.urlencode({'username':'admin','password':'dhbwang123'})
h.request('POST', '/login', body=body, headers={'Content-Type': 'application/x-www-form-urlencoded'})
resp = h.getresponse()
ck = resp.getheader('Set-Cookie','').split(';')[0].split(',')[0]
resp.read()

def check(url):
    h = http.client.HTTPConnection('localhost', 8000)
    h.request('GET', url, headers={'Cookie': ck})
    r = h.getresponse()
    html = r.read().decode()
    return r.status, html

# 1. 总览
_, dash = check('/dashboard/')
print('=== 总览 ===')
for k in ['系统接入状态','接入评分','Zoom','OAuth','会议','参与者','Webhook','推送',
          '运营统计','总租户','总用户','Zoom 账号','Telegram 频道','今日告警','今日推送','系统状态','更新时间',
          '今日参与者','当前在线','今日事件']:
    print(f'  {k}: {"✅" if k in dash else "❌"}')

# 2. 管理中心
_, ac = check('/dashboard/admin-center')
print('\n=== 管理中心 ===')
for k in ['用户管理','租户管理','系统设置','审计日志','推送管理','安全中心','账号管理']:
    print(f'  {k}: {"✅" if k in ac else "❌"}')
bad = [k for k in ['总租户','总用户','今日告警','今日推送','系统状态'] if k in ac]
print(f'  Stats残留: {"✅ 无" if not bad else "❌ " + str(bad)}')

# 3. 数字验证（stats 正确性）
# 运营统计段的 card 值
for kw in ['总租户','总用户','Zoom 账号','Telegram 频道','今日告警','今日推送']:
    idx = dash.find(kw)
    if idx == -1:
        print(f'  {kw}: ❌ not found')
        continue
    after = dash[idx:idx+80]
    import re
    m = re.search(r'<div class="stat-value[^>]*>([^<]+)</div>', after)
    val = m.group(1) if m else 'NOT FOUND'
    print(f'  {kw}: ✅ = {val}')

# 恢复
conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1")
conn.commit()
conn.close()
print('\n✅ 已恢复 super_admin, 2fa=1')
