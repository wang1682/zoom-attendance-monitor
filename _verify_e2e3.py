#!/usr/bin/env python3
"""正确验证：用 HTTPRedirectHandler 避免自动跟踪 redirect 丢失 cookie"""
import urllib.request, urllib.parse, http.cookiejar, sqlite3

DB = '/app/data/tracking.db'

# 降级
conn = sqlite3.connect(DB)
conn.execute("UPDATE users SET role='admin', telegram_2fa_enabled=0 WHERE id=1")
conn.commit()
conn.close()

# 正确方式：用 CookieJar
cj = http.cookiejar.CookieJar()
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(cj),
    urllib.request.HTTPRedirectHandler()
)
data = urllib.parse.urlencode({'username':'admin','password':'dhbwang123'}).encode()
resp = opener.open('http://localhost:8000/login', data)
print(f'1. Login: {resp.status} {resp.url}')
resp.read()

# 访问 dashboard
r2 = opener.open('http://localhost:8000/dashboard')
html = r2.read().decode()
print(f'2. Dashboard: {r2.status} url={r2.url} len={len(html)}')
print(f'   系统接入状态: {"✅" if "系统接入状态" in html else "❌"}')
print(f'   运营统计: {"✅" if "运营统计" in html else "❌"}')
print(f'   总租户: {"✅" if "总租户" in html else "❌"}')
print(f'   今日告警: {"✅" if "今日告警" in html else "❌"} ({html.count("今日告警")}次)')
print(f'   今日推送: {"✅" if "今日推送" in html else "❌"}')
print(f'   总用户: {"✅" if "总用户" in html else "❌"}')
print(f'   Zoom 账号: {"✅" if "Zoom 账号" in html else "❌"}')
print(f'   Telegram 频道: {"✅" if "Telegram 频道" in html else "❌"}')
print(f'   系统状态: {"✅" if "系统状态" in html else "❌"}')

# 访问 admin center
r3 = opener.open('http://localhost:8000/dashboard/admin-center')
ac = r3.read().decode()
print(f'3. Admin center: {r3.status} len={len(ac)}')
for c in ['用户管理','租户管理','系统设置','审计日志','推送管理','安全中心','账号管理']:
    print(f'   {c}: {"✅" if c in ac else "❌"}')
bad = [k for k in ['总租户','总用户','Zoom 账号','Telegram 频道','今日告警','今日推送','系统状态'] if k in ac]
print(f'   Stats残留: {"✅ 无" if not bad else "❌ " + str(bad)}')

# 恢复
conn = sqlite3.connect(DB)
conn.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1")
conn.commit()
conn.close()
print(f'\n4. 已恢复: super_admin, 2fa=1')
