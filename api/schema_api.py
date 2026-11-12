"""Data dictionary and legacy detection placeholder."""
from __future__ import annotations
import asyncio
from typing import Any, Dict
from fastapi import APIRouter, HTTPException
from tools.finance.schema_info import get_schema_info

fin_router = APIRouter(prefix="/api/v1", tags=["schema"])

@fin_router.get("/schema")
async def get_schema():
    """Return database schema info (data dictionary)"""
    try:
        return {"schema": await asyncio.to_thread(lambda: get_schema_info().to_prompt_text())}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"schema 加载失败: {e}")


@fin_router.post("/detect")
async def finance_detect(request: Dict[str, Any]):
    """Financial anomaly detection (M3 placeholder)"""
    return {"status": "todo", "message": "财务异常检测 M3 开发中"}
