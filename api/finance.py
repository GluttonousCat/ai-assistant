# -*- encoding: utf-8 -*-
"""
财务分析 API
- POST /api/v1/query         自然语言财务查询 (Text-to-SQL, 一次性返回)
- POST /api/v1/query/stream  同上, SSE 流式返回 (阶段进展 + 结果解读逐块输出)
- GET  /api/v1/schema        数据字典
- GET  /api/v1/chains                    产业链列表 (beta_alpha)
- GET  /api/v1/chains/{id}/analysis      链条量化分析 (无 LLM, 环节指标+成员)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from agent.fin_graph import invoke_financial_agent
from agent.intent import get_intent_classifier
from skills.base import SkillContext
from skills.fin_query.skill import FinQuerySkill
from skills.report.skill import ReportSkill
from tools.finance.schema_info import get_schema_info

fin_router = APIRouter(prefix="/api/v1", tags=["finance"])


# ============================================================
# 产业链 (beta_alpha 模块只读端点, 供前端「产业链」页面)
# ============================================================

@fin_router.get("/chains")
async def list_chains():
    """种子链列表 (轻量, 无 DB)"""
    from beta_alpha.analysis import chain_analysis as ca

    def _build():
        return [{
            "chain_id": c["chain_id"],
            "name": c.get("name", c["chain_id"]),
            "desc": c.get("desc", ""),
            "drivers": c.get("drivers", []),
            "node_count": len(c.get("nodes", [])),
        } for c in ca.load_chains()]

    return {"chains": await asyncio.to_thread(_build)}


@fin_router.get("/chains/{chain_id}/analysis")
async def get_chain_analysis(chain_id: str, node: Optional[str] = None):
    """链条量化分析 (三源映射+环节指标, 无 LLM; node 参数可只看单环节)"""
    from beta_alpha.analysis import chain_analysis as ca

    def _run():
        chain = ca.get_chain(chain_id)
        if not chain:
            return None
        node_obj = None
        if node:
            node_obj = next((n for n in chain.get("nodes", [])
                             if n["id"] == node), None)
        res = ca.map_chain_members(chain, node_filter=node_obj)
        cols, rows = ca.nodes_to_table(res)
        for n in res["nodes"]:
            n["members"] = n["members"][:10]   # 页面载荷瘦身
        return {"chain": res["chain"], "benchmark": res["benchmark"],
                "nodes": res["nodes"], "table": {"columns": cols, "rows": rows}}

    result = await asyncio.to_thread(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=f"产业链不存在: {chain_id}")
    return result


@fin_router.post("/query")
async def finance_query(request: Dict[str, Any]):
    """自然语言查询财务/行情数据 (session_id 提供时支持跨轮追问继承股票)"""
    text = (request.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text 不能为空")
    session_id = request.get("session_id")

    try:
        result = invoke_financial_agent(text, session_id=session_id)
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


# ============================================================
# SSE 流式查询
# ============================================================

def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


async def finance_query_stream(request: Dict[str, Any]):
    """
    SSE 流式: 阶段进展 + 结构化结果 + 结果解读逐块输出.
    事件:
      stage  {stage, message}            意图识别/SQL生成/执行等阶段
      data   {intent, data, columns, sql, rows}   结构化查询结果 (一次性)
      delta  {text}                      解读文本块 (多次)
      done   {}                           结束
      error  {message}                    失败
    """
    text = (request.get("text") or "").strip()
    if not text:
        yield _sse("error", {"message": "text 不能为空"})
        return
    session_id = request.get("session_id")

    # 1. 意图识别 (快, 一次性)
    try:
        clf = get_intent_classifier()
        intent_result = clf.classify(text)
        intent = intent_result.intent
    except Exception:
        intent = "unknown"
    yield _sse("stage", {"stage": "intent", "message": f"意图识别: {intent}"})

    try:
        if intent in ("query", "compare"):
            # 2. Text-to-SQL 链路 (SQL 生成/校验/执行不可流式; 解读流式)
            #    会话上下文: 追问省略主语时沿用上轮股票
            from agent.context_store import (get_session_stocks,
                                             update_session_from_result)
            skill = FinQuerySkill()
            ctx = SkillContext(user_input=text)
            context_stocks = get_session_stocks(session_id)
            if context_stocks:
                ctx.params = {"context_stocks": context_stocks}
            from skills.fin_query.prompts import RESULT_INTERPRET_PROMPT

            sql, explanation, tables = skill._generate_sql(text, context_stocks)
            if not sql:
                yield _sse("error", {"message": skill._friendly_parse_error(text)})
                return
            yield _sse("stage", {"stage": "sql", "message": "SQL 已生成, 校验执行中…"})

            from tools.finance.sql_guard import validate_sql, force_limit
            guard = validate_sql(sql)
            if not guard.valid:
                yield _sse("error", {"message": f"SQL 校验失败: {guard.error}"})
                return
            try:
                df, elapsed = skill._execute_sql(sql)
            except Exception as e:
                yield _sse("error", {"message": f"查询执行失败: {e}"})
                return
            yield _sse("stage", {"stage": "exec",
                                 "message": f"查询返回 {len(df)} 行 ({elapsed:.1f}s), 生成解读…"})

            data_records = df.to_dict("records")
            # 写回会话上下文 (本轮提及股票才更新, 支持后续追问)
            update_session_from_result(session_id, text, data_records)

            yield _sse("data", {
                "intent": intent,
                "data": data_records,
                "columns": list(df.columns),
                "sql": sql,
                "rows": len(df),
            })

            # 3. 流式解读
            import pandas as pd
            if df is None or df.empty:
                yield _sse("delta", {"text": "查询无结果。可能原因：该报告期无数据、"
                                             "股票代码不正确、或筛选条件过严。"})
            elif skill.llm:
                prompt = RESULT_INTERPRET_PROMPT.format(
                    user_query=text, explanation=explanation,
                    result_table=df.head(20).to_string(), max_rows=20)
                try:
                    for chunk in skill.llm.stream(prompt):
                        yield _sse("delta", {"text": chunk})
                except Exception as e:
                    from skills.fin_query.skill import _result_brief
                    yield _sse("delta", {"text": _result_brief(df)})
            else:
                from skills.fin_query.skill import _result_brief
                yield _sse("delta", {"text": _result_brief(df)})

        elif intent == "report":
            # 研报链路: 综合解读流式
            yield _sse("stage", {"stage": "report", "message": "检索研报并生成解读…"})
            skill = ReportSkill()
            params = dict(intent_result.slots or {})
            ctx = skill(SkillContext(user_input=text, params=params))
            if ctx.error:
                yield _sse("error", {"message": ctx.error})
                return
            result = ctx.result or {}
            if isinstance(result, dict):
                if result.get("reports"):
                    yield _sse("data", {"intent": "report",
                                        "report_count": result.get("report_count"),
                                        "target": result.get("target")})
                # 已生成的完整解读直接一次性发出 (analyze 内部聚合多篇, 不做逐 token 流)
                yield _sse("delta", {"text": result.get("summary", "")})
        elif intent == "chain":
            # 产业链Beta链路 (编排下沉在 beta_alpha.streaming, 此处薄包装)
            from beta_alpha.streaming import stream_chain
            async for chunk in stream_chain(text, _sse):
                yield chunk

        elif intent == "alpha":
            # 个股预期差链路 (同上)
            from beta_alpha.streaming import stream_alpha
            async for chunk in stream_alpha(text, _sse):
                yield chunk

        else:
            yield _sse("delta", {"text":
                "无法识别意图。支持的问法示例：\n"
                "- 查询平安银行的营收\n- 看看贵州茅台的毛利率\n- 解读中芯国际的研报\n- 看看黄金的观点\n"
                "- AI算力产业链有哪些环节\n- 中际旭创的预期差"})
    except Exception as e:
        yield _sse("error", {"message": f"Agent 执行失败: {e}"})

    yield _sse("done", {})


@fin_router.post("/query/stream")
async def finance_query_stream_endpoint(request: Dict[str, Any]):
    return StreamingResponse(
        finance_query_stream(request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


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