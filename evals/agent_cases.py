# -*- encoding: utf-8 -*-
"""
Agent chat eval case set (v1 baseline)

Target under test: agent.loop.run_agent behind /api/v1/agent/stream (real LLM + real PG)
Unlike the old evals/dataset.py (intent routing + SQL pipeline), intent recognition
has been replaced by the Agent tool loop; this case set asserts user-visible behavior:
  - tools_any       at least one of these tools must be called this turn (fast-lane hits count as query_financials)
  - tools_none      these tools must not appear (guards against rescan abuse etc.)
  - expect_no_tools no tool calls for the whole turn (common-sense Q&A)
  - contains_any    answer contains at least one keyword; contains_all requires all
  - not_contains    answer must not contain these (a global check for tool-record leakage also applies)

Type coverage: query/compare/mainbiz/valuation/forecast/verify/risk/chain/alpha/quant/
               knowledge (pure common sense) / no_data (future periods) / offscope (symbols outside DB) / multiturn (multi-turn follow-ups)
"""
from __future__ import annotations

AGENT_CASES = [
    # ---------- Single turn: financials/quotes ----------
    # Note: fast-lane direct hits go through RESULT_INTERPRET_PROMPT v2 (values are
    # rendered as charts and the interpretation does not restate data), so assertions
    # do not enforce specific values/units, only topic words and tool paths
    {"id": "q_maotai_rev", "type": "query", "turns": [
        {"q": "查询贵州茅台最近三年的营业收入",
         "tools_any": ["query_financials"],
         "contains_any": ["茅台", "营收", "收入"]}]},
    {"id": "q_zhongji_pe", "type": "query", "turns": [
        {"q": "中际旭创现在的市盈率是多少",
         "tools_any": ["query_financials"],
         "contains_any": ["中际旭创", "市盈率"]}]},
    {"id": "cmp_maotai_wly", "type": "compare", "turns": [
        {"q": "贵州茅台和五粮液谁的毛利率高",
         "tools_any": ["query_financials"],
         "contains_any": ["茅台", "五粮液", "%", "高"]}]},

    # ---------- Single turn: extended domains ----------
    {"id": "mb_zhongji", "type": "mainbiz", "turns": [
        {"q": "中际旭创靠什么赚钱",
         "tools_any": ["query_main_business"],
         "contains_any": ["光模块", "收入", "占比"]}]},
    {"id": "val_catl", "type": "valuation", "turns": [
        {"q": "宁德时代现在的估值贵不贵",
         "tools_any": ["valuation_percentile"], "contains_any": ["分位", "%"]}]},
    {"id": "fc_smic", "type": "forecast", "turns": [
        {"q": "中芯国际的券商盈利预测怎么看",
         "tools_any": ["get_forecasts", "search_reports", "analyze_alpha"],
         "contains_any": ["预测", "覆盖"]}]},
    {"id": "vf_sungrow", "type": "verify", "turns": [
        {"q": "阳光电源的券商预测准不准",
         "tools_any": ["verify_forecasts"], "contains_any": ["兑现", "偏差", "%"]}]},
    {"id": "risk_zhongji", "type": "risk", "turns": [
        {"q": "中际旭创有没有财务风险",
         "tools_any": ["detect_financial_risk"],
         "contains_any": ["风险", "现金流", "毛利率"]}]},

    # ---------- Single turn: chain/alpha ----------
    {"id": "chain_overview", "type": "chain", "turns": [
        {"q": "AI算力产业链有哪些环节",
         "tools_any": ["analyze_chain", "list_chains"], "contains_any": ["环节"]}]},
    {"id": "chain_node", "type": "chain", "turns": [
        {"q": "光模块环节有哪些受益标的",
         "tools_any": ["analyze_chain"], "contains_any": ["标的", "光模块"]}]},
    {"id": "alpha_zhongji", "type": "alpha", "turns": [
        {"q": "中际旭创的预期差怎么样",
         "tools_any": ["analyze_alpha"], "contains_any": ["预期", "分歧"]}]},

    # ---------- Compound intents (regression cases for the fast-lane deep-water keyword guard) ----------
    # Pass signal: valuation_percentile must be called (the fast lane never calls it;
    # its presence proves the Agent loop answered in full; query_financials is unconstrained)
    {"id": "cmpd_pe_and_val", "type": "compound", "turns": [
        {"q": "中际旭创的最新市盈率和估值分位一起看看",
         "tools_any": ["valuation_percentile"],
         "contains_any": ["市盈率", "估值", "分位"]}]},

    # ---------- Identity/single-dimension (after tool self-resolution optimization: no resolve_stock prefix needed) ----------
    {"id": "ent_industry_profile", "type": "identity", "turns": [
        {"q": "宁德时代属于哪个行业板块",
         "tools_any": ["resolve_stock"],
         "contains_any": ["电池", "电力", "行业", "宁德"]}]},
    {"id": "fc_single_dim_direct", "type": "forecast", "turns": [
        {"q": "阳光电源的券商预测历史兑现得怎么样",
         "tools_any": ["verify_forecasts"],
         "contains_any": ["兑现", "偏差", "%"]}]},

    # ---------- Common sense/boundaries ----------
    {"id": "kn_what_is_pe", "type": "knowledge", "turns": [
        {"q": "什么是市盈率", "expect_no_tools": True,
         "contains_any": ["市盈率", "市值", "净利润"]}]},
    {"id": "nd_future_period", "type": "no_data", "turns": [
        {"q": "贵州茅台2026年年报的净利润是多少",
         "contains_any": ["无结果", "尚未", "未披露", "暂无", "最新"],
         "not_contains": ["2026年年报净利润为"]}]},
    {"id": "off_us_stock", "type": "offscope", "turns": [
        {"q": "特斯拉的营收是多少",
         "contains_any": ["未识别", "无法", "暂无", "没有", "不支持", "美股"]}]},

    # ---------- Multi-turn follow-ups (core scenario) ----------
    {"id": "mt_qoq_challenge", "type": "multiturn", "turns": [
        {"q": "中际旭创2025年报的净利润是多少",
         "tools_any": ["query_financials"],
         "contains_any": ["中际旭创", "净利润"]},
        {"q": "环比竟然下降了，数据对吗？",
         "tools_any": ["query_financials"],
         "contains_any": ["累计", "单季", "口径"]}]},
    {"id": "mt_valuation_follow", "type": "multiturn", "turns": [
        {"q": "贵州茅台最新的市盈率多少",
         "tools_any": ["query_financials"],
         "contains_any": ["茅台", "市盈率"]},
        {"q": "那它的估值分位呢",
         "tools_any": ["valuation_percentile"],
         "contains_all": ["分位", "茅台"]}]},
    {"id": "mt_chain_to_alpha", "type": "multiturn", "turns": [
        {"q": "AI算力产业链有哪些环节",
         "tools_any": ["analyze_chain", "list_chains"], "contains_any": ["环节"]},
        {"q": "光模块环节的中际旭创预期差怎么样",
         "tools_any": ["analyze_alpha"], "contains_any": ["预期", "分歧"]}]},
]

# Enforced on every answer (regression guard): tool records must never leak to the user
GLOBAL_NOT_CONTAINS = ["[本轮工具调用]"]
