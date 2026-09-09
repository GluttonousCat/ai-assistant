# -*- encoding: utf-8 -*-
"""
Agent 对话评测用例集 (v1 基线)

评测对象: /api/v1/agent/stream 对应的 agent.loop.run_agent (真实 LLM + 真实 PG)
与旧 evals/dataset.py (意图路由+SQL 管道) 的区别: 意图识别已被 Agent 工具循环取代,
本用例集断言的是用户可感知行为:
  - tools_any      本轮至少调用了其中一个工具 (快车道命中记为 query_financials)
  - tools_none     这些工具不得出现 (防重扫描滥用等)
  - expect_no_tools 整轮不调任何工具 (常识问答)
  - contains_any   回答含关键词之一; contains_all 全部含
  - not_contains   回答不得含 (全局另强制检查工具记录泄漏)

类型覆盖: query/compare/mainbiz/valuation/forecast/verify/risk/chain/alpha/quant/
          knowledge(纯常识)/no_data(未来期间)/offscope(库外标的)/multiturn(多轮追问)
"""
from __future__ import annotations

AGENT_CASES = [
    # ---------- 单轮: 财务/行情 ----------
    # 注: 快车道直连走 RESULT_INTERPRET_PROMPT v2 (数值由图表呈现, 解读不复述数据),
    # 故断言不强制具体数值单位, 只断言主题词与工具路径
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

    # ---------- 单轮: 延伸域 ----------
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

    # ---------- 单轮: 产业链/Alpha/量化 ----------
    {"id": "chain_overview", "type": "chain", "turns": [
        {"q": "AI算力产业链有哪些环节",
         "tools_any": ["analyze_chain", "list_chains"], "contains_any": ["环节"]}]},
    {"id": "chain_node", "type": "chain", "turns": [
        {"q": "光模块环节有哪些受益标的",
         "tools_any": ["analyze_chain"], "contains_any": ["标的", "光模块"]}]},
    {"id": "alpha_zhongji", "type": "alpha", "turns": [
        {"q": "中际旭创的预期差怎么样",
         "tools_any": ["analyze_alpha"], "contains_any": ["预期", "分歧"]}]},
    {"id": "quant_regime", "type": "quant", "turns": [
        {"q": "贵州茅台现在处于什么形态",
         "tools_any": ["run_quant_scan", "compute_indicators"],
         "contains_any": ["区间", "趋势", "形态", "震荡"]}]},

    # ---------- 复合意图 (快车道深水区词防护的回归用例) ----------
    # 判定信号: valuation_percentile 必须被调用 (快车道永远不会调它,
    # 它在即证明走了 Agent 循环完整作答; query_financials 合法与否不约束)
    {"id": "cmpd_pe_and_val", "type": "compound", "turns": [
        {"q": "中际旭创的最新市盈率和估值分位一起看看",
         "tools_any": ["valuation_percentile"],
         "contains_any": ["市盈率", "估值", "分位"]}]},

    # ---------- 身份/单维度 (工具自解析优化后: 无需 resolve_stock 前置) ----------
    {"id": "ent_industry_profile", "type": "identity", "turns": [
        {"q": "宁德时代属于哪个行业板块",
         "tools_any": ["resolve_stock"],
         "contains_any": ["电池", "电力", "行业", "宁德"]}]},
    {"id": "fc_single_dim_direct", "type": "forecast", "turns": [
        {"q": "阳光电源的券商预测历史兑现得怎么样",
         "tools_any": ["verify_forecasts"],
         "contains_any": ["兑现", "偏差", "%"]}]},

    # ---------- 常识/边界 ----------
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

    # ---------- 多轮追问 (核心场景) ----------
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

# 所有回答强制检查 (防回归): 工具记录不得泄漏给用户
GLOBAL_NOT_CONTAINS = ["[本轮工具调用]"]
