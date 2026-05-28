#!/usr/bin/env python3
"""
start_webhook.py — Webhook 接收服务（端口 9000）
"""
from app.main import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=9000, log_level="info")
