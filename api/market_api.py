"""Minute bars are served from PG, never fetched from suppliers in HTTP requests."""

import asyncio
import re
from datetime import date
from uuid import UUID
from typing import Literal
from fastapi import APIRouter, HTTPException, Query
from tools.market.queries import query_intraday, stock_industries
from skills.trend.service import dashboard, freshness
from skills.trend.chart_history import stock_chart as stock_detail
from skills.trend.tracking import follow_up, follow_up_chart
from skills.trend.episodes import episode_tracking
from skills.trend.intraday import dashboard as intraday_dashboard, chart as intraday_chart

market_router = APIRouter(prefix="/api/v1/market", tags=["market"])


def _validate_stock_code(code: str):
    if not re.fullmatch(r"\d{6}\.(SH|SZ|BJ)", code):
        raise HTTPException(422, "股票代码必须包含SH/SZ/BJ后缀")


@market_router.get("/industries")
async def market_industries():
    """Current SW level-2 display metadata; not historical signal inputs."""
    return await asyncio.to_thread(stock_industries)


@market_router.get("/intraday")
async def market_intraday():
    result = await asyncio.to_thread(intraday_dashboard)
    return result or {"data": [], "as_of": None, "note": "尚未发布盘中观察结果，等待离线采集与扫描完成"}


@market_router.get("/intraday/{code}")
async def market_intraday_chart(code: str, run_id: str = Query(pattern=r"^\d{4}-\d{2}-\d{2}_(1030|1130|1400|1500)$"), timeframe: Literal["1D", "60F"] = "60F"):
    _validate_stock_code(code)
    try:
        return await asyncio.to_thread(intraday_chart, code, run_id, timeframe)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@market_router.get("/tracking")
async def signal_tracking(signal_date: date | None = None):
    try:
        return await asyncio.to_thread(follow_up, signal_date)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@market_router.get("/tracking/episodes")
async def signal_episodes(view: Literal["active", "archive"] = "active"):
    return await asyncio.to_thread(episode_tracking, view)


@market_router.get("/tracking/{code}")
async def signal_tracking_chart(
    code: str, run_id: UUID, timeframe: Literal["1D", "60F"] = "1D",
):
    _validate_stock_code(code)
    try:
        return await asyncio.to_thread(follow_up_chart, code, str(run_id), timeframe)
    except LookupError as error:
        raise HTTPException(404, str(error)) from error


@market_router.get("/freshness")
async def market_freshness():
    """Warehouse, published cutoff and background progress; never fetch providers."""
    return await asyncio.to_thread(freshness)


@market_router.get("/dashboard")
async def trend_dashboard(min_numeric_score: int | None = Query(
    default=None, ge=0, le=100, description="仅对 JSON 数字评分预筛。历史格式由客户端按原规则处理。",
)):
    result = await asyncio.to_thread(dashboard, min_numeric_score=min_numeric_score)
    if result is None:
        raise HTTPException(503, "尚未生成趋势快照，请运行 python -m jobs.scan_trend")
    return result


@market_router.get("/dashboard/{code}")
async def trend_stock(
    code: str, run_id: UUID | None = None, timeframe: Literal["60F", "1D"] = "60F"
):
    _validate_stock_code(code)
    snapshot = await asyncio.to_thread(
        dashboard, str(run_id) if run_id else None, False
    )
    if snapshot is None:
        raise HTTPException(503, "尚未生成趋势快照，请运行 python -m jobs.scan_trend")
    if timeframe == "1D" and "1D" not in snapshot["metadata"].get("timeframes", {}):
        raise HTTPException(404, "该历史快照尚未计算日K策略")
    cutoff = (
        snapshot["metadata"]
        .get("timeframes", {})
        .get(timeframe, {})
        .get("as_of", snapshot["as_of"])
    )
    return await asyncio.to_thread(
        stock_detail,
        code,
        snapshot["metadata"]["start"],
        cutoff,
        snapshot["metadata"]["config"],
        timeframe,
    )


@market_router.get("/bars")
async def bars(
    code: str = Query(pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
    start: date = date(2026, 1, 1),
    end: date = date(2026, 12, 31),
    period: int = 60,
    limit: int = Query(default=1000, ge=1, le=1000),
):
    if period not in (5, 15, 30, 60) or start > end:
        raise HTTPException(422, "周期或日期范围不合法")
    frame = await asyncio.to_thread(
        query_intraday, code, str(start), str(end), period, limit
    )
    # PG NUMERIC remains decimal text, avoiding silent rounding in serialization.
    import json
    import pandas as pd

    data = frame.astype(object).where(pd.notna(frame), None).to_dict(orient="records")
    return {
        "columns": list(frame.columns),
        "data": json.loads(json.dumps(data, default=str)),
        "rows": len(frame),
        "time_zone": "Asia/Shanghai",
        "volume_unit": "share",
        "amount_unit": "CNY",
    }
