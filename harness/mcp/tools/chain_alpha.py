# -*- encoding: utf-8 -*-
"""
产业链 + Alpha 域工具 (5个):
- list_chains     种子链清单 (支持哪些产业链)
- analyze_chain   链条量化分析 (环节->标的映射 + 环节指数超额收益)
- forge_chain     LLM 生成新种子链模板 (写文件, 需审核)
- analyze_alpha   个股预期差四象限 (预测分歧x财务动量x估值分位x研报观点)
- industry_peers  同行业比对 (申万二级成分股 估值x成长x盈利 横向排名)
"""
from __future__ import annotations

from typing import Any, Dict

from harness.mcp.registry import REGISTRY
from harness.mcp.spec import ToolError, obj_schema, param
from harness.mcp.tools._common import _jsonable, resolve_one


# ============================================================
# 11. list_chains
# ============================================================

@REGISTRY.tool(
    name="list_chains",
    domain="chain",
    description=(
        "列出平台当前支持的产业链种子链清单 (链名/简介/驱动因素/环节数)。"
        "用户问'有哪些产业链可以查'/'你们支持什么链', 或 analyze_chain 拿不准链名时先调本工具。"),
    params_schema=obj_schema({}),
    examples=["有哪些产业链", "支持哪些链条分析"],
)
def list_chains() -> Dict[str, Any]:
    from skills.beta_alpha.analysis import chain_analysis as ca
    chains = ca.load_chains()
    return {"count": len(chains), "chains": [{
        "chain_id": c["chain_id"], "name": c.get("name", c["chain_id"]),
        "desc": c.get("desc", ""), "drivers": c.get("drivers", []),
        "nodes": [n.get("name", n["id"]) for n in c.get("nodes", [])],
    } for c in chains]}


# ============================================================
# 12. analyze_chain
# ============================================================

@REGISTRY.tool(
    name="analyze_chain",
    domain="chain",
    description=(
        "产业链量化分析: 解析链/环节 -> 三源映射 (主营构成占比/申万行业/研报证据) 环节->标的, "
        "计算各环节指数近期超额收益与研报热度。可整链分析, 也可只看单个环节。"
        "用户问 'AI算力链有哪些环节'/'光模块环节的受益标的'/'XX链哪个环节最强' 时调用。"
        "纯量化无 LLM, 不生成观点综述。"),
    params_schema=obj_schema({
        "chain": param("产业链名称或主题话术 (如 'AI算力'/'人形机器人')", "string"),
        "node": param("只看某环节 (如 '光模块'), 可选; 缺省整链", "string"),
        "member_limit": param("每环节返回成员数", "integer", default=6),
    }, ["chain"]),
    examples=["AI算力产业链有哪些环节", "光模块环节的受益标的", "半导体国产化链哪个环节最强"],
)
def analyze_chain(chain: str, node: str = "", member_limit: int = 6) -> Dict[str, Any]:
    from skills.beta_alpha.analysis import chain_analysis as ca
    chain_obj = ca.match_chain(chain)
    if not chain_obj:
        raise ToolError(f"未匹配到产业链模板: {chain}。可先调 list_chains 查看支持清单, "
                        f"或用 forge_chain 生成新链")
    node_obj = ca.find_node(chain_obj, node) if node else None
    if node and not node_obj:
        names = "、".join(n.get("name", n["id"]) for n in chain_obj.get("nodes", []))
        raise ToolError(f"链「{chain_obj['name']}」下无环节「{node}」。现有环节: {names}")
    try:
        result = ca.map_chain_members(chain_obj, node_filter=node_obj)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"链条分析失败: {e}")
    cols, rows = ca.nodes_to_table(result)
    for n in result["nodes"]:
        n["members"] = n["members"][:max(1, int(member_limit))]
    data = [{k: _jsonable(v) for k, v in r.items()} for r in rows]
    return {"chain": result["chain"], "benchmark": result.get("benchmark"),
            "node_filter": result.get("node_filter"),
            "nodes": result["nodes"],
            "table": {"columns": cols, "rows": len(rows), "data": data}}


# ============================================================
# 13. forge_chain
# ============================================================

@REGISTRY.tool(
    name="forge_chain",
    domain="chain",
    description=(
        "用 LLM 生成新的产业链种子链模板 (YAML): 给一个主题 (如 '固态电池'), "
        "自动草拟环节/关键词/代表性标的, 用研报语料做命中率校验并自动修正。"
        "当 list_chains 里没有用户要的链时调用。注意: 会生成 YAML 草稿供人工审核, "
        "确认前不落盘 (save 由人工执行), 单次调用约 1-3 分钟。"),
    params_schema=obj_schema({
        "theme": param("产业链主题 (如 '固态电池'/'低空经济')", "string"),
    }, ["theme"]),
    examples=["帮我建一条固态电池产业链", "生成低空经济链模板"],
    read_only=False,
    notes="调 LLM 多轮 (草稿->校验->修正), 慢; 产物是 YAML 草稿, 不自动写文件",
)
def forge_chain(theme: str) -> Dict[str, Any]:
    from skills.beta_alpha.forge import chain_to_yaml, forge_chain as _forge
    try:
        res = _forge(theme)
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"种子链生成失败: {e}")
    if not res.get("chain"):
        errs = res.get("errors") or ["未知错误"]
        raise ToolError("结构校验未通过: " + "; ".join(errs))
    return {"theme": theme, "rounds": res.get("rounds"),
            "yaml": chain_to_yaml(res["chain"]),
            "hit_report": res.get("report")}


# ============================================================
# 14. analyze_alpha
# ============================================================

@REGISTRY.tool(
    name="analyze_alpha",
    domain="chain",
    description=(
        "个股预期差四象限综合分析 (纯量化): 象限1 券商盈利预测分歧度与上修/下修方向, "
        "象限2 财务边际动量 (营收/净利/毛利率 提速/降速/拐点), "
        "象限3 PE/PB 三年估值分位, 象限4 近期研报观点。"
        "用户问 'XX值不值得跟踪'/'市场对XX的预期'/'XX有预期差吗' 这类**综合**问题时调用; "
        "只要单一维度 (仅预测/仅兑现/仅估值水位) 就用对应工具: get_forecasts / "
        "verify_forecasts / valuation_percentile。"
        "返回结构化指标, 综合判断由 Agent 完成。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
    }, ["stock"]),
    examples=["中际旭创的预期差", "市场对贵州茅台的预期", "宁德时代基本面拐点"],
)
def analyze_alpha(stock: str) -> Dict[str, Any]:
    from skills.beta_alpha.skills.alpha import AlphaSkill
    hit = resolve_one(stock)
    skill = AlphaSkill()
    try:
        res = skill.analyze(hit["name"], hit["ts_code"])
    except Exception as e:  # noqa: BLE001
        raise ToolError(f"预期差分析失败: {e}")
    cols, data = skill.build_table(res)
    # quadrants 内 momentum 含 Timestamp/float, 统一清洗
    mom = res.get("momentum") or []
    res["momentum"] = [{k: _jsonable(v) for k, v in r.items()} for r in mom]
    return {"stock": hit, "quadrants": res,
            "table": {"columns": cols, "rows": len(data),
                      "data": [{k: _jsonable(v) for k, v in r.items()}
                               for r in data]}}


# ============================================================
# 15. industry_peers 同行业比对
# ============================================================

@REGISTRY.tool(
    name="industry_peers",
    domain="chain",
    description=(
        "同行业比对: 该股申万二级行业的成分股横向对比(市值/PE/PB/ROE/净利同比/毛利率), "
        "并给出目标股各项指标的行业排名。用户问 'XX和同行业比怎么样'/'行业里谁最便宜'/"
        "'XX在行业内什么水平' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "limit": param("返回同行数量 (按市值)", "integer", default=10),
    }, ["stock"]),
    examples=["中际旭创和同行比估值", "白酒行业里谁ROE最高"],
)
def industry_peers(stock: str, limit: int = 10) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        base = pg.fetch_one(
            "SELECT industry_l2 FROM stock.stock_basic WHERE ts_code=%s",
            (hit["ts_code"],))
        if not base or not base["industry_l2"]:
            raise ToolError(f"{hit['name']} 无申万行业数据")
        rows = pg.fetch_all(
            "WITH peers AS ( "
            "  SELECT ts_code, name FROM stock.stock_basic "
            "  WHERE industry_l2=%s ), "
            "mv AS (SELECT DISTINCT ON (ts_code) ts_code, pe_ttm, pb, total_mv "
            "       FROM stock.daily_basic "
            "       WHERE ts_code IN (SELECT ts_code FROM peers) "
            "       ORDER BY ts_code, trade_date DESC), "
            "fi AS (SELECT DISTINCT ON (ts_code) ts_code, roe, netprofit_yoy, "
            "              grossprofit_margin "
            "       FROM fin.fina_indicator "
            "       WHERE ts_code IN (SELECT ts_code FROM peers) "
            "             AND report_type='1' "
            "       ORDER BY ts_code, end_date DESC) "
            "SELECT p.ts_code, p.name, mv.pe_ttm, mv.pb, mv.total_mv, "
            "       fi.roe, fi.netprofit_yoy, fi.grossprofit_margin "
            "FROM peers p LEFT JOIN mv ON mv.ts_code=p.ts_code "
            "             LEFT JOIN fi ON fi.ts_code=p.ts_code",
            (base["industry_l2"],))
    if not rows:
        raise ToolError(f"{hit['name']} 所在行业 {base['industry_l2']} 无成分数据")

    def _f(v):
        return (round(float(v), 2) if v is not None
                and hasattr(v, "quantize") else v)

    data = [{k: _f(v) for k, v in r.items()} for r in rows]
    data.sort(key=lambda r: (r["total_mv"] is None,
                             -(r["total_mv"] or 0)))

    def _rank(metric: str, higher_is_better: bool = True):
        vals = [(d[metric], d["ts_code"]) for d in data
                if isinstance(d.get(metric), (int, float))]
        if hit["ts_code"] not in {c for _, c in vals}:
            return None
        vals.sort(key=lambda x: x[0], reverse=higher_is_better)
        for i, (_, c) in enumerate(vals, 1):
            if c == hit["ts_code"]:
                return {"rank": i, "of": len(vals)}

    return {"stock": hit, "sw_l2": base["industry_l2"],
            "peers_total": len(data), "total_mv_unit": "万元",
            "data": data[:int(limit)],
            "target_ranks": {"total_mv": _rank("total_mv"),
                             "pe_ttm": _rank("pe_ttm", higher_is_better=False),
                             "pb": _rank("pb", higher_is_better=False),
                             "roe": _rank("roe"),
                             "netprofit_yoy": _rank("netprofit_yoy")}}
