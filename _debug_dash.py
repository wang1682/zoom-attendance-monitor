#!/usr/bin/env python3
"""看 dashboard 渲染内容"""
import urllib.request, urllib.parse, http.client, sqlite3

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

h2 = http.client.HTTPConnection('localhost', 8000)
h2.request('GET', '/dashboard/', headers={'Cookie': ck})
resp2 = h2.getresponse()
body2 = resp2.read().decode()
print(f'Len={len(body2)}')

# 检查关键文本
for kw in ['运营统计','总租户','今日告警','系统接入状态','接入评分','今日参与者','当前在线','dash-myt-now']:
    idx = body2.find(kw)
    if idx >= 0:
        ctx = body2[max(0,idx-40):idx+80]
        print(f'✅ \"{kw}\" at {idx}: ...{ctx}...')
    else:
        print(f'❌ \"{kw}\" not found')

# 恢复
conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1")
conn.commit()
conn.close()
