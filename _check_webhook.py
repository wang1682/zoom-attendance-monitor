import sqlite3, datetime
conn = sqlite3.connect('/app/data/tracking.db')
conn.row_factory = sqlite3.Row
cutoff = (datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(hours=24)).isoformat()
c = conn.execute('SELECT COUNT(*) AS c FROM zoom_events WHERE created_at >= ?', (cutoff,))
total = c.fetchone()['c']
c = conn.execute('SELECT COUNT(*) AS c FROM zoom_events WHERE created_at >= ? AND tenant_id=?', (cutoff, 'wangtest'))
wt = c.fetchone()['c']
c = conn.execute('SELECT COUNT(*) FROM zoom_events')
all_total = c.fetchone()[0]
c = conn.execute('SELECT COUNT(*) FROM zoom_events WHERE tenant_id=?', ('wangtest',))
wt_total = c.fetchone()[0]

print(f"All tenants last 24h: {total}")
print(f"wangtest last 24h: {wt}")
print(f"=== webhook check shows: {'PASS' if total > 0 else 'FAIL'} (BUG: not tenant-filtered)")
print(f"=== wangtest-specific: {'PASS' if wt > 0 else 'FAIL'}")
print(f"All time totals - all: {all_total}, wangtest: {wt_total}")

# Check the WANG account webhook details
c = conn.execute("SELECT id, account_id, webhook_secret, webhook_last_event, webhook_last_time, status FROM zoom_accounts WHERE tenant_id='wangtest'")
a = c.fetchone()
if a:
    print(f"\nWANG account: id={a['id']}, secret={'SET' if a['webhook_secret'] else 'EMPTY'}")
    print(f"  last_event={a['webhook_last_event']}, last_time={a['webhook_last_time']}")
    print(f"  status={a['status']}")
