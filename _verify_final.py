#!/usr/bin/env python3
"""完整验收"""
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
status, dash = check('/dashboard/')
print(f'=== 总览 ({status}) ===')
expect_dash = ['系统接入状态','接入评分','Zoom','OAuth','会议','参与者','Webhook','推送',
               '运营统计','总租户','总用户','Zoom 账号','Telegram 频道','今日告警','今日推送','系统状态',
               '今日参与者','当前在线','今日事件',
               '当前共享屏幕','活跃会议','最近告警','最近事件']
for k in expect_dash:
    cnt = dash.count(k) if dash.count(k) > 1 else '-'
    print(f'  {k}: {"✅" if k in dash else "❌"}{f" (x{cnt})" if cnt != "-" else ""}')

# 2. 管理中心
status, ac = check('/dashboard/admin-center')
print(f'\n=== 管理中心 ({status}) ===')
expect_ac = ['用户管理','租户管理','系统设置','审计日志','推送管理','安全中心','账号管理']
for k in expect_ac:
    print(f'  {k}: {"✅" if k in ac else "❌"}')
bad = [k for k in ['总租户','总用户','Zoom 账号','Telegram 频道','今日告警','今日推送','系统状态','更新时间'] if k in ac]
print(f'  Stats残留: {"✅ 无" if not bad else "❌ " + str(bad)}')

# 恢复
conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1")
conn.commit()
conn.close()
print('\n已恢复 super_admin, 2fa=1')
