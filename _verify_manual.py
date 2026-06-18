#!/usr/bin/env python3
"""手动跟踪 redirect, 不自动跟随"""
import urllib.request, http.cookiejar, urllib.parse, sqlite3, http.client

DB = '/app/data/tracking.db'

def with_conn(fn):
    conn = sqlite3.connect(DB)
    try:
        fn(conn)
        conn.commit()
    finally:
        conn.close()

# 降级
with_conn(lambda c: c.execute("UPDATE users SET role='admin', telegram_2fa_enabled=0 WHERE id=1"))

# 手动 HTTP 请求
conn = http.client.HTTPConnection('localhost', 8000)
headers = {'Content-Type': 'application/x-www-form-urlencoded'}
body = urllib.parse.urlencode({'username':'admin','password':'dhbwang123'})
conn.request('POST', '/login', body=body, headers=headers)
resp = conn.getresponse()
print(f'LOGIN POST: {resp.status}')
print(f'Location: {resp.getheader("Location","")}')
cookies = resp.getheader('Set-Cookie', '')
print(f'Set-Cookie: {cookies[:60]}')
resp.read()

# 用 cookie 手动请求 dashboard
conn2 = http.client.HTTPConnection('localhost', 8000)
conn2.request('GET', '/dashboard', headers={'Cookie': cookies.split(';')[0]})
resp2 = conn2.getresponse()
body = resp2.read().decode()
print(f'\nDASHBOARD GET: {resp2.status} url={resp2.getheader("Location","")}')
print(f'Len={len(body)}')
print(f'Has 总租户: {"✅" if "总租户" in body else "❌"}')
print(f'Has 系统接入状态: {"✅" if "系统接入状态" in body else "❌"}')
print(f'Has 运营统计: {"✅" if "运营统计" in body else "❌"}')
print(f'Has 用户管理: {"✅" if "用户管理" in body else "❌"}')

# 恢复
with_conn(lambda c: c.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1"))
with_conn(lambda c: print(f'恢复: {c.execute("SELECT role,telegram_2fa_enabled FROM users WHERE id=1").fetchone()}'))
