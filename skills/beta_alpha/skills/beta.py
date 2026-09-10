"""
产业链 Beta 挖掘 Skill (v1)

编排: 请求解析(链/环节, 规则优先+LLM兜底) -> chain_analysis 三源映射+环节指标 -> LLM 中文综述
量化全部在 beta_alpha/analysis/chain_analysis (无 LLM 依赖), 本层只做编排与表达.

用法(命令行验证):
    python -m agent.beta_alpha.skills.beta AI算力产业链
"""
from __future__ import annotations

import json
from typing import Any, Dict, Optional

from core.logger import get_logger
from core.llm.client import get_agent_llm
from skills.base import BaseSkill, SkillContext
from skills.beta_alpha.analysis import chain_analysis as ca
from skills.beta_alpha.prompts import (
    BETA_CHAIN_PICK_PROMPT,
    BETA_SUMMARY_PROMPT,
    BETA_NODE_PROMPT,
)

logger = get_logger(__name__)


def _extract_json(text: str) -> Optional[Dict]:
    """宽容 JSON 提取 (剥 ```json 块)"""
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


class BetaSkill(BaseSkill):
    """产业链 Beta 挖掘: 趋势 -> 环节 -> 标的映射 + 环节行情验证"""

    name = "beta"
    description = ("产业链Beta挖掘: 解析用户趋势话术到种子链模板, 三源映射"
                   "(主营构成/申万行业/研报)环节->标的, 计算环节指数超额收益")
    intent_keywords = ["产业链", "链条", "环节", "beta"]

    def __init__(self, llm: Optional[Any] = None):
        self.llm = llm or get_agent_llm()

    # ---------- 请求解析 ----------

    def resolve_request(self, text: str) -> Dict:
        """用户话术 -> {chain, node} | {error}"""
        chain = ca.match_chain(text)
        if chain:
            return {"chain": chain, "node": ca.find_node(chain, text)}

        # LLM 兜底: 从模板清单选链
        chains_desc = "\n".join(
            f"- {c['chain_id']} | {c.get('name', '')} | "
            f"环节: {'、'.join(n.get('name', n['id']) for n in c.get('nodes', []))}"
            for c in ca.load_chains()
        )
        try:
            resp = self.llm.invoke(
                BETA_CHAIN_PICK_PROMPT.format(chains=chains_desc, text=text))
            parsed = _extract_json(resp) or {}
            chain_id = parsed.get("chain_id")
            node_id = parsed.get("node_id")
        except Exception as e:
            logger.warning(f"BetaSkill 链解析 LLM 失败: {e}")
            chain_id, node_id = None, None

        chain = ca.get_chain(chain_id) if chain_id else None
        if not chain:
            names = "、".join(c.get("name", c["chain_id"]) for c in ca.load_chains())
            return {"error": f"未找到匹配的产业链模板。当前支持: {names}。"
                             "可在 beta_alpha/chains/ 下新增 YAML 模板扩展。"}
        node = next((n for n in chain.get("nodes", []) if n["id"] == node_id), None)
        return {"chain": chain, "node": node}

    # ---------- 分析 ----------

    def analyze(self, chain: Dict, node: Optional[Dict]) -> Dict:
        """三源映射 + 环节指标 (纯量化, 不含 LLM)"""
        return ca.map_chain_members(chain, node_filter=node)

    def build_summary_prompt(self, result: Dict) -> str:
        """链分析结果 -> LLM 综述 prompt"""
        chain = result["chain"]
        if result.get("node_filter"):
            node = next(n for n in result["nodes"])
            m = node.get("metrics") or {}
            members = [
                {k: mm[k] for k in ("name", "tier", "mainbz_share",
                                    "mainbz_items", "report_hits")}
                for mm in node.get("members", [])[:10]
            ]
            return BETA_NODE_PROMPT.format(
                chain_name=chain["name"], node_name=node["name"],
                keywords="、".join(node["keywords"]),
                metrics=json.dumps(m, ensure_ascii=False),
                members=json.dumps(members, ensure_ascii=False),
                heat=node.get("report_heat_6m", 0),
            )

        cols, rows = ca.nodes_to_table(result)
        node_table = json.dumps(rows, ensure_ascii=False)
        members = []
        for n in result["nodes"]:
            for mm in n["members"][:4]:
                members.append({
                    "环节": n["name"], "标的": mm["name"], "tier": mm["tier"],
                    "mainbz_share": mm["mainbz_share"], "证据": mm["evidence"],
                })
        return BETA_SUMMARY_PROMPT.format(
            chain_name=chain["name"], chain_desc=chain.get("desc", ""),
            drivers="、".join(chain.get("drivers", [])),
            benchmark=result.get("benchmark", ""),
            node_table=node_table,
            members=json.dumps(members, ensure_ascii=False),
        )

    # ---------- Skill 入口 ----------

    def run(self, context: SkillContext) -> SkillContext:
        req = self.resolve_request(context.user_input)
        if req.get("error"):
            context.error = req["error"]
            return context
        try:
            result = self.analyze(req["chain"], req["node"])
        except Exception as e:
            logger.error(f"BetaSkill 分析失败: {e}")
            context.error = f"产业链分析失败: {e}"
            return context

        cols, rows = ca.nodes_to_table(result)
        # SSE data 载荷瘦身: 成员截到前 6
        for n in result["nodes"]:
            n["members"] = n["members"][:6]
        context.result = {
            "chain": result["chain"],
            "nodes": result["nodes"],
            "node_filter": result.get("node_filter"),
            "data": rows,
            "columns": cols,
            "rows": len(rows),
        }

        try:
            context.result["summary"] = self.llm.invoke(
                self.build_summary_prompt(result))
        except Exception as e:
            logger.warning(f"BetaSkill 综述生成失败: {e}")
            context.result["summary"] = "量化数据已生成, 但综述生成失败。"
        return context


if __name__ == "__main__":
    import sys
    text = sys.argv[1] if len(sys.argv) > 1 else "AI算力产业链"
    ctx = BetaSkill()(SkillContext(user_input=text))
    if ctx.error:
        print("ERROR:", ctx.error)
    else:
        cols, rows = ctx.result["columns"], ctx.result["data"]
        import pandas as pd
        print(pd.DataFrame(rows, columns=cols).to_string(index=False))
        print("\n---- 综述 ----\n", ctx.result.get("summary", "")[:800])
