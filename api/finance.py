# -*- encoding: utf-8 -*-
"""
Financial analysis API
- POST /api/v1/agent/stream  Agent chat entry (LLM autonomous decisions + MCP tool loop, multi-turn follow-ups)
- GET  /api/v1/schema        Data dictionary
- GET  /api/v1/chains                    Industry chain list (beta_alpha)
- GET  /api/v1/chains/{id}/analysis      Chain quantitative analysis (no LLM, node metrics + members)
"""
from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from tools.finance.schema_info import get_schema_info

fin_router = APIRouter(prefix="/api/v1", tags=["finance"])


# ============================================================
# Industry chains (read-only endpoints of the beta_alpha module, for the frontend "Chains" page)
# ============================================================

@fin_router.get("/chains")
async def list_chains():
    """Seed chain list (lightweight, no DB)"""
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
    """Chain quantitative analysis (three-source mapping + node metrics, no LLM; the node parameter scopes to a single node)"""
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
            n["members"] = n["members"][:10]   # trim page payload
        return {"chain": res["chain"], "benchmark": res["benchmark"],
                "nodes": res["nodes"], "table": {"columns": cols, "rows": rows}}

    result = await asyncio.to_thread(_run)
    if result is None:
        raise HTTPException(status_code=404, detail=f"产业链不存在: {chain_id}")
    return result


@fin_router.post("/chains/forge/stream")
async def forge_chain_stream_endpoint(request: Dict[str, Any]):
    """LLM seed-chain generation (SSE): draft -> structure validation -> hit rate -> revision; the data event carries the YAML draft + report"""
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
    """Save a seed chain (edited LLM draft / user-written; structure validated server-side)"""
    from pathlib import Path as _P

    from skills.beta_alpha.forge import save_chain_yaml
    yaml_text = request.get("yaml") or ""
    ok, errors, path = await asyncio.to_thread(save_chain_yaml, yaml_text, "web")
    if not ok:
        raise HTTPException(status_code=400, detail="; ".join(errors))
    return {"saved": True, "chain_id": _P(path).stem, "path": path}


# ============================================================
# SSE streaming query
# ============================================================

def _sse(event: str, data: Any) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n"


# ============================================================
# Agent chat entry (LLM autonomous decisions + MCP tool loop, new paradigm)
# ============================================================

@fin_router.post("/agent/stream")
async def agent_stream_endpoint(request: Dict[str, Any]):
    """Agent chat SSE (free-form questions + multi-turn follow-ups)

    Request: {text, session_id} — session_id is generated by the frontend and
    carried throughout (multi-turn context)
    Events: stage / tool_call / data(intent=agent) / delta / done / error
    Sandbox: 29 read-only tools, per-tool timeout, round limit (see harness/loop.py)
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
        # Underlying modules (scanners) may print; under SSE, stdout must carry only the event stream
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
    """Return database schema info (data dictionary)"""
    info = get_schema_info()
    try:
        return {"schema": info.to_prompt_text()}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"schema 加载失败: {e}")


@fin_router.post("/detect")
async def finance_detect(request: Dict[str, Any]):
    """Financial anomaly detection (M3 placeholder)"""
    return {"status": "todo", "message": "财务异常检测 M3 开发中"}