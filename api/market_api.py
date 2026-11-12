"""Minute bars are served from PG, never fetched from suppliers in HTTP requests."""
import asyncio
from datetime import date
from fastapi import APIRouter, HTTPException, Query
from tools.market.queries import query_intraday

market_router = APIRouter(prefix="/api/v1/market", tags=["market"])


@market_router.get("/bars")
async def bars(code: str = Query(pattern=r"^\d{6}\.(SH|SZ|BJ)$"),
               start: date = date(2026, 1, 1), end: date = date(2026, 12, 31),
               period: int = 60, limit: int = Query(default=1000, ge=1, le=1000)):
    if period not in (5, 15, 30, 60) or start > end:
        raise HTTPException(422, "周期或日期范围不合法")
    frame = await asyncio.to_thread(query_intraday, code, str(start), str(end), period, limit)
    # PG NUMERIC remains decimal text, avoiding silent rounding in serialization.
    import json
    import pandas as pd
    data = frame.astype(object).where(pd.notna(frame), None).to_dict(orient="records")
    return {"columns": list(frame.columns), "data": json.loads(json.dumps(data, default=str)),
            "rows": len(frame), "time_zone": "Asia/Shanghai", "volume_unit": "share", "amount_unit": "CNY"}
