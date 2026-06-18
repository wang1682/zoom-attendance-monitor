#!/usr/bin/env python3
"""检查当前 DB 状态"""
import sqlite3

conn = sqlite3.connect('/app/data/tracking.db')
r = conn.execute('SELECT id, username, role, telegram_2fa_enabled, password_hash FROM users WHERE id=1').fetchone()
print(f'{r[0]} | {r[1]} | role={r[2]} | 2fa={r[3]} | hash={r[4][:40]}')
conn.close()
