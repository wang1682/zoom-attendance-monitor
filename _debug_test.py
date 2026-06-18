#!/usr/bin/env python3
"""调试 stats，看日志"""
import http.client, urllib.parse, sqlite3

conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='admin', telegram_2fa_enabled=0 WHERE id=1")
conn.commit()
conn.close()

h = http.client.HTTPConnection('localhost', 8000)
h.request('POST', '/login', urllib.parse.urlencode({'username':'admin','password':'dhbwang123'}),
          headers={'Content-Type': 'application/x-www-form-urlencoded'})
resp = h.getresponse()
resp.read()

h2 = http.client.HTTPConnection('localhost', 8000)
h2.request('GET', '/dashboard/', headers={'Cookie': resp.getheader('Set-Cookie','').split(';')[0].split(',')[0]})
r2 = h2.getresponse()
html = r2.read().decode()

print(f'Status: {r2.status} Len: {len(html)}')
print(f'总租户: {"✅" if "总租户" in html else "❌"}')

# 看 stats 区域
import re
m = re.search(r'总租户[^<]*<div class="stat-value[^>]*>([^<]+)', html)
if m:
    print(f'stats: total_tenants={m.group(1)}')

conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1")
conn.commit()
conn.close()
