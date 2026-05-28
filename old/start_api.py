#!/usr/bin/env python3
"""
start_api.py — REST API 服务（端口 8000）
"""
from app.main import app

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000, log_level="info")
