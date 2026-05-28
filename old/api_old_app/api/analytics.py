"""analytics_api.py — 分析数据 API 路由 (FastAPI)
挂载到 /api/v2/analytics 前缀

Phase 8: 所有端点从 request.state.tenant_id 读取租户（由 API Token 中间件注入）
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_async_db, SyncSession

router = APIRouter(prefix="/api/v2/analytics", tags=["analytics"])


def _r(row) -> dict:
    """将 SQLAlchemy Row 转为普通 dict"""
    return dict(row._mapping)


def _get_tenant_id(request: Request) -> str:
    """从 request.state 获取 tenant_id（由 API Token 中间件注入）"""
    tid = getattr(request.state, "tenant_id", "default")
    return tid


@router.get("/daily")
async def list_daily_stats(
    request: Request,
    date: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_async_db),
):
    """每日聚合统计"""
    tenant_id = _get_tenant_id(request)

    sql = "SELECT * FROM daily_stats WHERE tenant_id = :tenant_id"
    params = {"tenant_id": tenant_id}
    if date:
        sql += " AND date = :date"
        params["date"] = date
    sql += " ORDER BY date DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = (await session.execute(text(sql), params)).fetchall()
    items = [_r(r) for r in rows]

    count_sql = "SELECT COUNT(*) FROM daily_stats WHERE tenant_id = :tenant_id"
    count_params = {"tenant_id": tenant_id}
    if date:
        count_sql += " AND date = :date"
        count_params["date"] = date
    total = (await session.execute(text(count_sql), count_params)).scalar() or 0

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/hourly")
async def list_hourly_activity(
    request: Request,
    date: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_async_db),
):
    """时段活跃度统计"""
    tenant_id = _get_tenant_id(request)

    sql = "SELECT * FROM hourly_activity WHERE tenant_id = :tenant_id"
    params = {"tenant_id": tenant_id}
    if date:
        sql += " AND date = :date"
        params["date"] = date
    sql += " ORDER BY date DESC, hour DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = (await session.execute(text(sql), params)).fetchall()
    items = [_r(r) for r in rows]

    count_sql = "SELECT COUNT(*) FROM hourly_activity WHERE tenant_id = :tenant_id"
    count_params = {"tenant_id": tenant_id}
    if date:
        count_sql += " AND date = :date"
        count_params["date"] = date
    total = (await session.execute(text(count_sql), count_params)).scalar() or 0

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/participants")
async def list_participant_stats(
    request: Request,
    date: Optional[str] = None,
    name: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_async_db),
):
    """个人维度统计"""
    tenant_id = _get_tenant_id(request)

    sql = "SELECT * FROM participant_daily_stats WHERE tenant_id = :tenant_id"
    params = {"tenant_id": tenant_id}
    if date:
        sql += " AND date = :date"
        params["date"] = date
    if name:
        sql += " AND name LIKE :name"
        params["name"] = f"%{name}%"
    sql += " ORDER BY date DESC, total_duration_minutes DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = (await session.execute(text(sql), params)).fetchall()
    items = [_r(r) for r in rows]

    count_sql = "SELECT COUNT(*) FROM participant_daily_stats WHERE tenant_id = :tenant_id"
    count_params = {"tenant_id": tenant_id}
    if date:
        count_sql += " AND date = :date"
        count_params["date"] = date
    if name:
        count_sql += " AND name LIKE :name"
        count_params["name"] = f"%{name}%"
    total = (await session.execute(text(count_sql), count_params)).scalar() or 0

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/risks")
async def list_risks(
    request: Request,
    date: Optional[str] = None,
    risk_type: Optional[str] = None,
    limit: int = Query(default=50, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_async_db),
):
    """风险记录"""
    tenant_id = _get_tenant_id(request)

    sql = "SELECT * FROM risk_scores WHERE tenant_id = :tenant_id"
    params = {"tenant_id": tenant_id}
    if date:
        sql += " AND date = :date"
        params["date"] = date
    if risk_type:
        sql += " AND risk_type = :risk_type"
        params["risk_type"] = risk_type
    sql += " ORDER BY date DESC, created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = (await session.execute(text(sql), params)).fetchall()
    items = [_r(r) for r in rows]

    count_sql = "SELECT COUNT(*) FROM risk_scores WHERE tenant_id = :tenant_id"
    count_params = {"tenant_id": tenant_id}
    if date:
        count_sql += " AND date = :date"
        count_params["date"] = date
    if risk_type:
        count_sql += " AND risk_type = :risk_type"
        count_params["risk_type"] = risk_type
    total = (await session.execute(text(count_sql), count_params)).scalar() or 0

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.get("/reports")
async def list_reports(
    request: Request,
    date: Optional[str] = None,
    limit: int = Query(default=10, le=50),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_async_db),
):
    """AI 日报列表"""
    tenant_id = _get_tenant_id(request)

    sql = "SELECT * FROM ai_reports WHERE tenant_id = :tenant_id"
    params = {"tenant_id": tenant_id}
    if date:
        sql += " AND date = :date"
        params["date"] = date
    sql += " ORDER BY created_at DESC LIMIT :limit OFFSET :offset"
    params["limit"] = limit
    params["offset"] = offset
    rows = (await session.execute(text(sql), params)).fetchall()
    items = [_r(r) for r in rows]

    count_sql = "SELECT COUNT(*) FROM ai_reports WHERE tenant_id = :tenant_id"
    count_params = {"tenant_id": tenant_id}
    if date:
        count_sql += " AND date = :date"
        count_params["date"] = date
    total = (await session.execute(text(count_sql), count_params)).scalar() or 0

    return {"items": items, "total": total, "limit": limit, "offset": offset}


@router.post("/reports/generate")
async def trigger_report(
    request: Request,
    date: Optional[str] = None,
    force: bool = Query(default=False),
    send: bool = Query(default=False),
):
    """手动触发日报生成（同步，可能耗时～20s）"""
    tenant_id = _get_tenant_id(request)
    try:
        from analytics.ai_report import generate_daily_report
        result = generate_daily_report(
            force=force,
            send=send,
            date_str=date,
            tenant_id=tenant_id,
        )
        if result:
            return {"ok": True, "report": {
                "id": result.get("id"),
                "date": result.get("date"),
                "ai_provider": result.get("ai_provider"),
                "sent": result.get("sent"),
                "summary": result.get("summary"),
            }}
        return {"ok": False, "error": "无数据或报告已存在"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
