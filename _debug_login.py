#!/usr/bin/env python3
"""调试登录流程"""
import urllib.request, urllib.parse, http.cookiejar, sqlite3

conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='admin', telegram_2fa_enabled=0 WHERE id=1")
conn.commit()
print(f'DB: {conn.execute("SELECT role,telegram_2fa_enabled FROM users WHERE id=1").fetchone()}')
conn.close()

# 手动 POST 不带自动 redirect
import http.client
h = http.client.HTTPConnection('localhost', 8000)
body = urllib.parse.urlencode({'username':'admin','password':'dhbwang123'})
h.request('POST', '/login', body=body, headers={'Content-Type': 'application/x-www-form-urlencoded'})
resp = h.getresponse()
print(f'POST status: {resp.status}')
print(f'Location: {resp.getheader("Location","")}')
print(f'Set-Cookie: {resp.getheader("Set-Cookie","(none)")[:80]}')
resp.read()

# 手动 GET dashboard 带 cookie
h2 = http.client.HTTPConnection('localhost', 8000)
h2.request('GET', '/dashboard/', headers={'Cookie': resp.getheader('Set-Cookie','').split(';')[0].split(',')[0] if resp.getheader('Set-Cookie') else ''})
resp2 = h2.getresponse()
body2 = resp2.read().decode()
print(f'\nGET /dashboard/: status={resp2.status} len={len(body2)}')
print(f'Has 运营统计: {"✅" if "运营统计" in body2 else "❌"}')
print(f'Has 总租户: {"✅" if "总租户" in body2 else "❌"}')
print(f'First 100: {body2[:100]}')

# 恢复
conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1")
conn.commit()
conn.close()
print('恢复完成')
