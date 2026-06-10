#!/bin/bash
# P0 Check Script - run from /opt/zoom-attendance-monitor

echo "=============================================="
echo "A. Docker Services Status"
echo "=============================================="
docker compose ps 2>/dev/null || docker-compose ps

echo ""
echo "=============================================="
echo "B1. Webhook Logs (last 50 lines)"
echo "=============================================="
docker compose logs --tail=50 zoom-webhook 2>/dev/null || docker-compose logs --tail=50 zoom-webhook

echo ""
echo "=============================================="
echo "B2. Command/Telegram Bot Logs (last 50 lines)"
echo "=============================================="
docker compose logs --tail=50 zoom-command 2>/dev/null || docker-compose logs --tail=50 zoom-command

echo ""
echo "=============================================="
echo "B3. API Service Logs (last 20 lines)"
echo "=============================================="
docker compose logs --tail=20 zoom-api 2>/dev/null || docker-compose logs --tail=20 zoom-api

echo ""
echo "=============================================="
echo "C. Database Record Counts"
echo "=============================================="
docker exec zoom-webhook python3 -c "
import sqlite3
db_paths = ['/app/data/tracking.db', '/app/data/monitor.db']
for p in db_paths:
    try:
        conn = sqlite3.connect(p)
        c = conn.cursor()
        c.execute(\"SELECT name FROM sqlite_master WHERE type='table'\")
        tables = c.fetchall()
        print(f'=== {p} ===')
        for t in tables:
            name = t[0]
            c.execute(f'SELECT COUNT(*) FROM \"{name}\"')
            cnt = c.fetchone()[0]
            print(f'  {name}: {cnt} rows')
        conn.close()
    except Exception as e:
        print(f'{p}: ERROR - {e}')
" 2>/dev/null || echo "docker exec zoom-webhook failed"

echo ""
echo "=============================================="
echo "D1. API Health Check"
echo "=============================================="
curl -s http://localhost:5000/api/v3/system/health 2>/dev/null | head -5 || echo "health endpoint failed"

echo ""
echo "=============================================="
echo "D2. API Dashboard"
echo "=============================================="
# Try with a session cookie
curl -s -b /tmp/zoom_cookie.txt http://localhost:5000/api/v3/dashboard 2>/dev/null | head -200 || echo "dashboard endpoint failed"

echo ""
echo "=============================================="
echo "D3. API Live"
echo "=============================================="
curl -s http://localhost:5000/api/v3/live 2>/dev/null | head -200 || echo "live endpoint failed"

echo ""
echo "=============================================="
echo "D4. Telegram Channels API"
echo "=============================================="
curl -s http://localhost:5000/api/v3/telegram/channels 2>/dev/null | head -200 || echo "telegram channels endpoint failed"

echo ""
echo "=============================================="
echo "D5. Telegram Rules API"
echo "=============================================="
curl -s http://localhost:5000/api/v3/telegram/rules 2>/dev/null | head -200 || echo "telegram rules endpoint failed"

echo ""
echo "=============================================="
echo "E. zoom_participants recent data"
echo "=============================================="
docker exec zoom-webhook python3 -c "
import sqlite3, json
try:
    conn = sqlite3.connect('/app/data/tracking.db')
    c = conn.cursor()
    # Check zoom_participants
    try:
        c.execute('SELECT COUNT(*) FROM zoom_participants')
        print(f'zoom_participants total: {c.fetchone()[0]}')
        c.execute('SELECT MAX(join_time) FROM zoom_participants')
        print(f'zoom_participants max join_time: {c.fetchone()[0]}')
        c.execute('SELECT * FROM zoom_participants ORDER BY join_time DESC LIMIT 3')
        for row in c.fetchall():
            print(f'  {row}')
    except Exception as e:
        print(f'zoom_participants: {e}')
    
    # Check zoom_events
    try:
        c.execute('SELECT COUNT(*) FROM zoom_events')
        print(f'zoom_events total: {c.fetchone()[0]}')
        c.execute('SELECT event_ts FROM zoom_events ORDER BY event_ts DESC LIMIT 3')
        print('zoom_events last event_ts:')
        for r in c.fetchall():
            print(f'  {r[0]}')
    except Exception as e:
        print(f'zoom_events: {e}')
    
    # Check sharing_live
    try:
        c.execute('SELECT COUNT(*) FROM sharing_live')
        print(f'sharing_live total: {c.fetchone()[0]}')
    except Exception as e:
        print(f'sharing_live: {e}')
    
    conn.close()
except Exception as e:
    print(f'Error: {e}')
" 2>/dev/null || echo "db query failed"

echo ""
echo "=============================================="
echo "F. .env Webhook Secret"
echo "=============================================="
grep -n "WEBHOOK_SECRET\|ZOOM_SECRET\|ZOOM_WEBHOOK" /opt/zoom-attendance-monitor/.env 2>/dev/null || echo "No webhook secret found in .env"

echo ""
echo "=============================================="
echo "G. Route grep from app.py"
echo "=============================================="
grep -n "@app\.route\|@app\.get\|@app\.post\|def api_v3\|def dashboard\|def live\|def event\|def tele\|def sharing\|def alert" app.py 2>/dev/null | head -40

echo ""
echo "=============================================="
echo "DONE"
echo "=============================================="
