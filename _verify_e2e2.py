#!/usr/bin/env python3
"""端到端验证：改DB → 验证 → 恢复DB"""
import urllib.request, http.cookiejar, urllib.parse, sqlite3, hashlib

DB = '/app/data/tracking.db'

def with_conn(fn):
    conn = sqlite3.connect(DB)
    try:
        fn(conn)
        conn.commit()
    finally:
        conn.close()

# Step 1: 降级 admin → 关闭 2FA
with_conn(lambda c: c.execute("UPDATE users SET role='admin', telegram_2fa_enabled=0 WHERE id=1"))

# Step 2: 验证 DB 已改
with_conn(lambda c: print('DB role/admin+2fa=0:', c.execute('SELECT role,telegram_2fa_enabled FROM users WHERE id=1').fetchone()))

# Step 3: 登录
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
data = urllib.parse.urlencode({'username':'admin','password':'dhbwang123'}).encode()
resp = opener.open('http://localhost:8000/login', data)
print(f'Login: {resp.status} {resp.getheader("Location","")}')
print(f'Cookies: {[c.name for c in cj]}')

# Step 4: Dashboard
try:
    r = opener.open('http://localhost:8000/dashboard')
    html = r.read().decode()
    print(f'\nDashboard: {r.status} len={len(html)} url={r.url}')
    print(f'  系统接入状态: {"✅" if "系统接入状态" in html else "❌"}')
    print(f'  运营统计: {"✅" if "运营统计" in html else "❌"}')
    print(f'  总租户: {"✅" if "总租户" in html else "❌"}')
    print(f'  今日告警: {"✅" if "今日告警" in html else "❌"} ({html.count("今日告警")}次)')
    print(f'  今日推送: {"✅" if "今日推送" in html else "❌"}')
    print(f'  总用户: {"✅" if "总用户" in html else "❌"}')
    print(f'  Zoom 账号: {"✅" if "Zoom 账号" in html else "❌"}')
    print(f'  Telegram 频道: {"✅" if "Telegram 频道" in html else "❌"}')
    print(f'  系统状态: {"✅" if "系统状态" in html else "❌"}')
    print(f'  今日参与者: {"✅" if "今日参与者" in html else "❌"}')
    print(f'  当前在线: {"✅" if "当前在线" in html else "❌"}')
except Exception as e:
    print(f'Dashboard error: {e}')

# Step 5: Admin center
try:
    r = opener.open('http://localhost:8000/dashboard/admin-center')
    ac = r.read().decode()
    print(f'\nAdmin center: {r.status} len={len(ac)}')
    for c in ['用户管理','租户管理','系统设置','审计日志','推送管理','安全中心','账号管理']:
        print(f'  {c}: {"✅" if c in ac else "❌"}')
    bad = [k for k in ['总租户','总用户','Zoom 账号','Telegram 频道','今日告警','今日推送','系统状态'] if k in ac]
    print(f'  Stats残留: {"✅ 无" if not bad else "❌ " + str(bad)}')
except Exception as e:
    print(f'Admin center error: {e}')

# Step 6: 恢复
with_conn(lambda c: c.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1"))
with_conn(lambda c: print(f'\n恢复: {c.execute("SELECT role,telegram_2fa_enabled FROM users WHERE id=1").fetchone()}'))
