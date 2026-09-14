# -*- encoding: utf-8 -*-
"""
财务分析 API
- POST /api/v1/agent/stream  Agent 对话入口 (LLM 自主决策 + MCP 工具循环, 多轮追问)
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

from tools.finance.schema_info import get_schema_info

fin_router = APIRouter(prefix="/api/v1", tags=["finance"])


# ============================================================
# 产业链 (beta_alpha 模块只读端点, 供前端「产业链」页面)
# ============================================================

@fin_router.get("/chains")
async def list_chains():
    """种子链列表 (轻量, 无 DB)"""
    from skills.beta_alpha.analysis import chain_analysis as ca

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
    from skills.beta_alpha.analysis import chain_analysis as ca

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


@fin_router.post("/chains/forge/stream")
async def forge_chain_stream_endpoint(request: Dict[str, Any]):
    """LLM 种子链生成 (SSE): 草稿->结构校验->命中率->修正; data 事件带 YAML 草稿+报告"""
    import threading

    theme = (request.get("theme") or "").strip()
    if not theme:
        raise HTTPException(status_code=400, detail="theme 不能为空")

    async def gen():
        from skills.beta_alpha.forge import chain_to_yaml, forge_chain
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue()
        box: Dict[str, Any] = {}

        def _stage(msg: str):
            loop.call_soon_threadsafe(q.put_nowait, ("stage", msg))

        def _work():
            try:
                box["res"] = forge_chain(theme, on_stage=_stage)
            except Exception as e:  # noqa: BLE001
                box["err"] = str(e)
            finally:
                loop.call_soon_threadsafe(q.put_nowait, ("end", None))

        threading.Thread(target=_work, daemon=True, name="chain-forge").start()
        while True:
            kind, payload = await q.get()
            if kind == "stage":
                yield _sse("stage", {"stage": "forge", "message": payload})
            else:
                break
        if box.get("err"):
            yield _sse("error", {"message": f"生成失败: {box['err']}"})
        elif not (box.get("res") or {}).get("chain"):
            errs = (box.get("res") or {}).get("errors") or ["未知错误"]
            yield _sse("error", {"message": "结构校验未通过: " + "; ".join(errs)})
        else:
            res = box["res"]
            yield _sse("data", {
                "intent": "forge",
                "yaml": chain_to_yaml(res["chain"]),
                "report": res["report"],
                "chain_id": res["chain"].get("chain_id"),
                "rounds": res["rounds"],
            })
        yield _sse("done", {})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@fin_router.post("/chains/save")
async def save_chain_endpoint(request: Dict[str, Any]):
    """保存种子链 (LLM 草稿编辑后/用户手写; 服务端结构校验)"""
    from pathlib import Path as _P

    from skills.beta_alpha.forge import save_chain_yaml
    yaml_text = request.get("yaml") or ""
    ok, errors, path = await asyncio.to_thread(save_chain_yaml, yaml_text, "web")
    if not ok:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {"saved": True, "chain_id": _P(path).stem, "path": path}


# ============================================================
# SSE 流式查询
# ============================================================

def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ============================================================
# Agent 对话入口 (LLM 自主决策 + MCP 工具循环, 新范式)
# ============================================================

@fin_router.post("/agent/stream")
async def agent_stream_endpoint(request: Dict[str, Any]):
    """Agent 对话 SSE (随意问 + 多轮追问)

    请求: {text, session_id}  — session_id 由前端生成并全程携带 (多轮上下文)
    事件: stage / tool_call / data(intent=agent) / delta / done / error
    沙盒: 只读 17 工具, 每工具超时, 轮数上限 (详见 agent/loop.py)
    """
    text = (request.get("text") or "").strip()
    if not text:
        return StreamingResponse(
            iter([_sse("error", {"message": "text 不能为空"}),
                  _sse("done", {})]),
            media_type="text/event-stream")
    session_id = request.get("session_id")

    async def gen():
        from harness.loop import run_agent_stream
        # 底层模块 (扫描器) 可能 print; SSE 下 stdout 必须只有事件流
        import contextlib
        import sys
        with contextlib.redirect_stdout(sys.stderr):
            try:
                async for chunk in run_agent_stream(text, session_id, _sse):
                    yield chunk
            except Exception as e:  # noqa: BLE001
                yield _sse("error", {"message": f"Agent 执行失败: {e}"})
        yield _sse("done", {})

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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