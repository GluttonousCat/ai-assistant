# -*- encoding: utf-8 -*-
"""
财务分析 API
- POST /api/v1/query    自然语言财务查询 (Text-to-SQL)
- GET  /api/v1/schema   数据字典
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException

from agent.fin_graph import invoke_financial_agent, classify_intent
from tools.finance.schema_info import get_schema_info

fin_router = APIRouter(prefix="/api/v1", tags=["finance"])


@fin_router.post("/query")
async def finance_query(request: Dict[str, Any]):
    """自然语言查询财务/行情数据"""
    text = (request.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")

    try:
        result = invoke_financial_agent(text)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Agent 执行失败: {e}")

    intent = (result.parsed_intent or {}).get("type", "unknown")

    payload: Dict[str, Any] = {
        "intent": intent,
        "response": result.response,
        "log": result.execution_log[-20:],
    }

    # 查询类附带结构化结果
    if result.fin_result:
        payload.update({
            "data": result.fin_result.get("data", []),
            "columns": result.fin_result.get("columns", []),
            "sql": result.fin_result.get("sql"),
            "rows": result.fin_result.get("rows", 0),
        })
    return payload


@fin_router.get("/schema")
async def get_schema():
    """返回数据库 schema 信息 (数据字典)"""
    info = get_schema_info()
    try:
        return {"schema": info.to_prompt_text()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"schema 加载失败: {e}")


@fin_router.post("/detect")
async def finance_detect(request: Dict[str, Any]):
    """财务异常检测 (M3 占位)"""
    return {"status": "todo", "message": "财务异常检测 M3 开发中"}