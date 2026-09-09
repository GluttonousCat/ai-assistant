# -*- encoding: utf-8 -*-
"""agent 对话循环单元测试 (不调 LLM/不连 DB)

运行: .venv\\Scripts\\python.exe -m pytest agent/tests -q
E2E (真实 LLM) 见 docs/agents/agent_loop.md 验证记录。
"""
from __future__ import annotations

from agent import history_store
from agent.loop import (DEFAULT_TOOL_TIMEOUT, MAX_ROUNDS, REPEAT_LIMIT,
                        TOOL_TIMEOUTS, _extract_table, build_system_prompt)


# ---------- 快车道 NL2SQL (LLM 主 / 规则兜底) ----------

def test_extract_sql_tolerant_shapes():
    """LLM 输出的 SQL 提取: 围栏/裸语句/带解释/垃圾输入"""
    from agent.loop import _extract_sql
    assert _extract_sql("SELECT 1") == "SELECT 1"
    assert _extract_sql("```sql\nSELECT 2\n```") == "SELECT 2"
    assert _extract_sql("```\nWITH t AS (SELECT 1) SELECT * FROM t\n```") \
        == "WITH t AS (SELECT 1) SELECT * FROM t"
    assert _extract_sql("好的, SQL 如下:\nSELECT a FROM b LIMIT 10;") \
        == "SELECT a FROM b LIMIT 10"
    assert _extract_sql("这不是 SQL") is None
    assert _extract_sql("") is None
    assert _extract_sql("UPDATE x SET y=1") is None      # 非 SELECT/WITH 拒绝


def test_alias_digest_non_empty():
    from agent.loop import _alias_digest
    d = _alias_digest()
    assert "营收" in d and "净利润" in d


def test_nl2sql_config_registered():
    """nl2sql 用途必须在 config 注册且关思考 (时延关键: 思考版实测 4~27s)"""
    from core.config import get_config
    from llm.client import get_nl2sql_llm
    cfg = get_config()
    assert cfg.llm_model_for("nl2sql"), "config.yaml llm.models.nl2sql 未配置"
    assert cfg.get("llm.models.nl2sql.enable_thinking") is False
    assert get_nl2sql_llm().purpose == "nl2sql"


def test_fast_lane_timeout_const():
    from agent.loop import NL2SQL_TIMEOUT
    assert 10 <= NL2SQL_TIMEOUT <= 60


# ---------- 系统提示词 ----------

def test_system_prompt_contains_domain_knowledge():
    p = build_system_prompt()
    # 口径知识 (环比质疑的关键)
    assert "累计口径" in p and "Q2单季" in p
    # 反幻觉纪律
    assert "必须来自工具" in p
    # 多轮规则
    assert "省略主语" in p or "历史" in p
    # 禁止输出工具过程信息给用户
    assert "绝不向用户输出工具调用记录" in p
    # 中文与免责
    assert "投资建议" in p


# ---------- 会话历史 ----------

def test_history_roundtrip_and_injection():
    history_store.reset("t-s1")
    history_store.append_user("t-s1", "中际旭创2025年报净利润")
    history_store.append_assistant(
        "t-s1", "净利润 107.97 亿",
        [{"name": "query_financials",
          "args": {"stocks": ["中际旭创"], "metrics": ["净利润"]},
          "ok": True, "elapsed_ms": 300, "rows": 12}])
    history_store.append_user("t-s1", "环比竟然下降了，数据对吗？")

    msgs = history_store.get_messages("t-s1")
    roles = [m["role"] for m in msgs]
    assert roles == ["user", "assistant", "user", "system"]
    # 工具摘要只进 system 后台记录, 绝不进 assistant 正文
    # (曾因拼在正文里被模型模仿, 输出"[本轮工具调用]"给用户)
    assert "[本轮工具调用]" not in msgs[1]["content"]
    assert "query_financials" not in msgs[1]["content"]
    record = msgs[-1]["content"]
    assert "query_financials" in record and "严禁" in record
    # 未定义 session 不炸, 空历史返回 []
    assert history_store.get_messages(None) == []
    assert history_store.get_messages("no-such") == []


def test_history_no_tool_record_without_tools():
    """没有工具调用的会话不注入后台记录 system 消息"""
    history_store.reset("t-s4")
    history_store.append_user("t-s4", "什么是市盈率")
    history_store.append_assistant("t-s4", "市盈率 = 市值 / 净利润 …", [])
    msgs = history_store.get_messages("t-s4")
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_history_injection_caps():
    """注入轮数上限生效 (超过 max_turns 只保留最近轮)"""
    history_store.reset("t-s2")
    for i in range(10):
        history_store.append_user("t-s2", f"问题{i}" * 10)
        history_store.append_assistant("t-s2", f"回答{i}" * 10, [])
    msgs = history_store.get_messages("t-s2", max_turns=3)
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert len(users) == 3
    assert "问题9" in users[-1]          # 最近的保留
    assert "问题0" not in users[0]       # 最早的被截掉


def test_history_ttl_cleanup():
    """过期会话被惰性清理 (直接改 ts 模拟 30 分钟前)"""
    import time
    history_store.reset("t-s3")
    history_store.append_user("t-s3", "旧会话")
    with history_store._LOCK:
        for t in history_store._STORE["t-s3"]:
            t["ts"] = time.time() - 3600
    history_store.append_user("t-tick", "触发清理")   # 触发惰性清理
    assert history_store.get_messages("t-s3") == []


# ---------- 沙盒参数 ----------

def test_sandbox_config():
    assert MAX_ROUNDS <= 10
    assert REPEAT_LIMIT >= 2
    assert TOOL_TIMEOUTS["run_quant_scan"] > DEFAULT_TOOL_TIMEOUT
    assert DEFAULT_TOOL_TIMEOUT >= 60


def test_tool_manifest_read_only():
    """对话循环只能拿到只读工具 (forge_chain 不可见)"""
    from mcp.bridge import openai_tools
    tools = openai_tools(include_write=False)
    names = {t["function"]["name"] for t in tools}
    assert "forge_chain" not in names
    assert len(names) == 18   # 18 只读 (含 build_company_profile)
    assert "query_financials" in names and "verify_forecasts" in names


# ---------- 表格提取 ----------

def test_extract_table_shapes():
    assert _extract_table({"columns": ["a"], "data": [{"a": 1}], "rows": 1})
    assert _extract_table({"table": {"columns": ["a"], "data": [{"a": 1}]}})
    assert _extract_table({"foo": 1}) is None
    assert _extract_table({"data": []}) is None
    assert _extract_table(None) is None


# ---------- 快车道 NL2SQL ----------

def test_extract_sql_shapes():
    from agent.loop import _extract_sql
    assert _extract_sql("SELECT 1").strip() == "SELECT 1"
    assert _extract_sql("```sql\nSELECT a FROM t WHERE x=1;\n```"
                         ).strip() == "SELECT a FROM t WHERE x=1"
    assert _extract_sql("好的, SQL 如下:\nWITH x AS (SELECT 1) SELECT * FROM x"
                         ).startswith("WITH")
    assert _extract_sql("抱歉我无法生成") is None
    assert _extract_sql("") is None
    assert _extract_sql("UPDATE t SET a=1") is None   # 非 SELECT/WITH 拒绝


def test_fast_lane_llm_fallback_to_rules():
    """LLM 生成失败/返回 None 时回退规则引擎 (需 DB, 无 LLM)"""
    import asyncio
    from agent import loop as loop_mod

    orig = loop_mod._llm_nl2sql
    loop_mod._llm_nl2sql = lambda *a, **k: None    # 模拟 LLM 失败
    try:
        events = []

        async def _run():
            def sse(ev, payload):
                events.append((ev, payload))
                return ""
            async for _ in loop_mod.run_agent_stream(
                    "查询贵州茅台的营收", "t-fallback", sse, fast_lane=True):
                pass

        asyncio.run(_run())
        data = next(p for e, p in events if e == "data")
        assert data["rows"] >= 1
        assert "SELECT" in data["sql"].upper()
        assert data["columns"][0] == "stock_name"
    finally:
        loop_mod._llm_nl2sql = orig


# ---------- 快车道判定 ----------

def test_fast_path_intent_hit_and_miss():
    from agent.loop import _fast_path_intent
    # 简单查询/对比 (核心词高置信 + 有股票实体) -> 快车道
    assert _fast_path_intent("查询贵州茅台的营收") == "query"
    assert _fast_path_intent("茅台和五粮液谁的毛利率高") == "compare"
    # 复合/其他域问题 -> Agent 循环 (None)
    assert _fast_path_intent("中际旭创的预期差") is None            # alpha 域
    assert _fast_path_intent("茅台的研报怎么说") is None             # report 域
    assert _fast_path_intent("帮我看看中际旭创有没有财务风险") is None
    assert _fast_path_intent("环比竟然下降了，数据对吗？") is None   # 追问口语
    # 无股票实体: 常识问题/库外标的不走快车道 (曾把"什么是市盈率"当数据查询硬跑)
    assert _fast_path_intent("什么是市盈率") is None
    assert _fast_path_intent("特斯拉的营收是多少") is None
    assert _fast_path_intent("白酒行业毛利率排名") is None           # 行业维度交 Agent
    # 深水区词: 复合意图/延伸域不走快车道 (曾把"市盈率和估值分位一起看看"截胡答一半)
    assert _fast_path_intent("中际旭创的最新市盈率和估值分位一起看看") is None
    assert _fast_path_intent("贵州茅台的营收分析一下") is None
    assert _fast_path_intent("中际旭创的研报怎么说") is None
    assert _fast_path_intent("") is None


# ---------- 并行执行 ----------

def test_execute_batch_parallel_and_ordered():
    """同轮多个工具调用应并行 (总耗时≈max 而非求和) 且结果保序"""
    import asyncio
    import time
    from agent import loop as loop_mod

    async def fake_run_tool(name, args):
        await asyncio.sleep(0.3)
        return {"ok": True, "tool": name, "data": args.get("i")}

    orig = loop_mod._run_tool
    loop_mod._run_tool = fake_run_tool
    try:
        calls = [("tool_a", {"i": 1}), ("tool_b", {"i": 2}), ("tool_c", {"i": 3})]
        t0 = time.time()
        results = asyncio.run(loop_mod._execute_batch(calls))
        elapsed = time.time() - t0
        assert [r["data"] for r in results] == [1, 2, 3]   # 保序
        assert elapsed < 0.8, f"疑似串行: {elapsed:.2f}s"    # 串行应 ~0.9s
    finally:
        loop_mod._run_tool = orig
