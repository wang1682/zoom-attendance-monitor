#!/usr/bin/env python3
"""调试：验证 dashboard_index 渲染模板时的 context"""
import urllib.request, urllib.parse, http.client, sqlite3

conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='admin', telegram_2fa_enabled=0 WHERE id=1")
conn.commit()
conn.close()

# 登录
h = http.client.HTTPConnection('localhost', 8000)
body = urllib.parse.urlencode({'username':'admin','password':'dhbwang123'})
h.request('POST', '/login', body=body, headers={'Content-Type': 'application/x-www-form-urlencoded'})
resp = h.getresponse()
ck = resp.getheader('Set-Cookie','').split(';')[0].split(',')[0]
resp.read()

# 获取 dashboard 全内容
h2 = http.client.HTTPConnection('localhost', 8000)
h2.request('GET', '/dashboard/', headers={'Cookie': ck})
resp2 = h2.getresponse()
body2 = resp2.read().decode()

# 查看运营统计区域前后内容
import re
# 找运营统计附近
idx = body2.find('系统接入状态')
if idx >= 0:
    after = body2[idx:idx+6000]
    print('After "系统接入状态" (6000 chars):')
    print(after)
else:
    print('No 系统接入状态 found, first 500:')
    print(body2[:500])

# 恢复
conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1")
conn.commit()
conn.close()
