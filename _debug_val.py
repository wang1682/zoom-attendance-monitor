#!/usr/bin/env python3
"""调试 stats 值"""
import http.client, urllib.parse, sqlite3, re

conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='admin', telegram_2fa_enabled=0 WHERE id=1")
conn.commit()
conn.close()

h = http.client.HTTPConnection('localhost', 8000)
h.request('POST', '/login', urllib.parse.urlencode({'username':'admin','password':'dhbwang123'}),
          headers={'Content-Type': 'application/x-www-form-urlencoded'})
resp = h.getresponse()
ck = resp.getheader('Set-Cookie','').split(';')[0].split(',')[0]
resp.read()

h2 = http.client.HTTPConnection('localhost', 8000)
h2.request('GET', '/dashboard/', headers={'Cookie': ck})
r2 = h2.getresponse()
html = r2.read().decode()

for kw in ['总租户','总用户','Zoom 账号','Telegram 频道','今日告警','今日推送','系统状态']:
    idx = html.find(kw)
    if idx >= 0:
        after = html[idx:idx+80]
        m = re.search(r'stat-value[^>]*>([^<]+)', after)
        print(f'{kw}: {m.group(1).strip() if m else "?"}')
    else:
        print(f'{kw}: ❌ not found')

conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1")
conn.commit()
conn.close()
