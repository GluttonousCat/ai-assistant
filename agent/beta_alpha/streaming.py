"""
beta_alpha SSE 流式编排 (api 层薄包装的消费端)

stream_chain / stream_alpha: 与 api/finance.py 的 query/report 分支同协议
(stage -> data -> delta* ; 不发 done, 由 api 统一收尾)
量化计算走 asyncio.to_thread, 避免同步重 IO 阻塞事件循环。
"""
from __future__ import annotations

import asyncio
from typing import AsyncIterator, Callable

from core.logger import get_logger
from agent.beta_alpha.analysis import chain_analysis as ca
from agent.beta_alpha.skills.alpha import AlphaSkill, _resolve_stock
from agent.beta_alpha.skills.beta import BetaSkill

logger = get_logger(__name__)

SseFormatter = Callable[[str, object], str]  # (event, data) -> SSE 帧文本


async def stream_chain(text: str, sse: SseFormatter) -> AsyncIterator[str]:
    """产业链Beta链路: 链解析 -> 三源映射+环节指标(线程) -> 综述流式"""
    skill = BetaSkill()
    yield sse("stage", {"stage": "chain", "message": "解析产业链意图…"})
    req = await asyncio.to_thread(skill.resolve_request, text)
    if req.get("error"):
        yield sse("delta", {"text": req["error"]})
        return
    chain, node = req["chain"], req["node"]
    focus = f", 聚焦环节: {node['name']}" if node else ""
    yield sse("stage", {"stage": "map",
                        "message": f"产业链: {chain['name']}{focus}, 环节映射中…"})
    try:
        result = await asyncio.to_thread(skill.analyze, chain, node)
    except Exception as e:
        yield sse("error", {"message": f"产业链分析失败: {e}"})
        return

    cols, rows = ca.nodes_to_table(result)
    n_members = sum(n["strong_count"] + n["medium_count"] for n in result["nodes"])
    yield sse("stage", {"stage": "compute",
                        "message": f"映射完成: {len(result['nodes'])} 个环节, "
                                   f"{n_members} 只标的, 生成解读…"})
    for n in result["nodes"]:
        n["members"] = n["members"][:6]
    yield sse("data", {
        "intent": "chain", "data": rows, "columns": cols,
        "rows": len(rows), "chain": result["chain"], "nodes": result["nodes"],
    })
    try:
        prompt = skill.build_summary_prompt(result)
        for chunk in skill.llm.stream(prompt):
            yield sse("delta", {"text": chunk})
    except Exception:
        yield sse("delta", {"text": "量化数据已生成, 综述生成失败。环节详情见上方表格。"})


async def stream_alpha(text: str, sse: SseFormatter) -> AsyncIterator[str]:
    """个股预期差链路: 股票解析 -> 四象限量化(线程) -> 综述流式"""
    yield sse("stage", {"stage": "stock", "message": "识别目标股票…"})
    hit = await asyncio.to_thread(_resolve_stock, text)
    if not hit:
        yield sse("delta", {"text": "未识别到股票。请带上股票名或代码, "
                                    "例如: 中际旭创的预期差 / 贵州茅台的分歧度"})
        return
    name, ts_code = hit
    yield sse("stage", {"stage": "compute",
                        "message": f"标的: {name}({ts_code}), 四象限计算中…"})
    skill = AlphaSkill()
    try:
        res = await asyncio.to_thread(skill.analyze, name, ts_code)
    except Exception as e:
        yield sse("error", {"message": f"预期差分析失败: {e}"})
        return
    cols, data = skill.build_table(res)
    yield sse("data", {"intent": "alpha", "data": data, "columns": cols,
                       "rows": len(data), "ts_code": ts_code, "name": name})
    try:
        prompt = skill.build_summary_prompt(res)
        for chunk in skill.llm.stream(prompt):
            yield sse("delta", {"text": chunk})
    except Exception:
        yield sse("delta", {"text": "四象限数据已生成, 综述生成失败。指标详情见上方表格。"})
