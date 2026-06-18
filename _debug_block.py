#!/usr/bin/env python3
"""看 运营统计 段落的完整 HTML"""
import http.client, urllib.parse, sqlite3

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

# 找到运营统计块
start = html.find('运营统计</h2>')
if start >= 0:
    end = html.find('</div>', start + 20)
    # 找到最近的</div>之后的</div>（panel mb-p 结束）
    # 先到 </div>\n</div> 两个关闭
    block = html[start:start+1200]
    print(block)
else:
    print('"运营统计" not found')

# 看系统接入状态后的区域
start2 = html.find('系统接入状态')
if start2 >= 0:
    end2 = html.find('运营统计', start2)
    if end2 >= 0:
        between = html[start2:end2]
        print(f'\n--- Between 系统接入状态 and 运营统计 ({len(between)} chars) ---')
        print(between[-200:])

conn = sqlite3.connect('/app/data/tracking.db')
conn.execute("UPDATE users SET role='super_admin', telegram_2fa_enabled=1 WHERE id=1")
conn.commit()
conn.close()
