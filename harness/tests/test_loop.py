# -*- encoding: utf-8 -*-
"""Agent conversation loop unit tests (no LLM calls / no DB).

Run: .venv\\Scripts\\python.exe -m pytest agent/tests -q
E2E (real LLM) see the verification records in docs/agents/agent_loop.md.
"""
from __future__ import annotations

from harness import session as history_store
from harness.loop import (DEFAULT_TOOL_TIMEOUT, MAX_ROUNDS, REPEAT_LIMIT,
                        TOOL_TIMEOUTS, _extract_table, build_system_prompt)


# ---------- Fast-lane NL2SQL (LLM primary / rule fallback) ----------

def test_extract_sql_tolerant_shapes():
    """SQL extraction from LLM output: fences / bare statement / with explanation / garbage."""
    from harness.loop import _extract_sql
    assert _extract_sql("SELECT 1") == "SELECT 1"
    assert _extract_sql("```sql\nSELECT 2\n```") == "SELECT 2"
    assert _extract_sql("```\nWITH t AS (SELECT 1) SELECT * FROM t\n```") \
        == "WITH t AS (SELECT 1) SELECT * FROM t"
    assert _extract_sql("好的, SQL 如下:\nSELECT a FROM b LIMIT 10;") \
        == "SELECT a FROM b LIMIT 10"
    assert _extract_sql("这不是 SQL") is None
    assert _extract_sql("") is None
    assert _extract_sql("UPDATE x SET y=1") is None      # non-SELECT/WITH rejected


def test_alias_digest_non_empty():
    from harness.loop import _alias_digest
    d = _alias_digest()
    assert "营收" in d and "净利润" in d


def test_nl2sql_config_registered():
    """The nl2sql purpose must be registered in config with thinking off (latency-critical: thinking variant measured 4~27s)."""
    from core.config import get_config
    from harness.llm import get_nl2sql_llm
    cfg = get_config()
    assert cfg.llm_model_for("nl2sql"), "config.yaml llm.models.nl2sql 未配置"
    assert cfg.get("llm.models.nl2sql.enable_thinking") is False
    assert get_nl2sql_llm().purpose == "nl2sql"


def test_fast_lane_timeout_const():
    from harness.loop import NL2SQL_TIMEOUT
    assert 10 <= NL2SQL_TIMEOUT <= 60


# ---------- System prompt ----------

def test_system_prompt_contains_domain_knowledge():
    p = build_system_prompt()
    # Caliber knowledge (key for quarter-over-quarter challenges)
    assert "累计口径" in p and "Q2单季" in p
    # Anti-hallucination discipline
    assert "必须来自工具" in p
    # Multi-turn rules
    assert "省略主语" in p or "历史" in p
    # Tool-process info must not be shown to users
    assert "绝不向用户输出工具调用记录" in p
    # Chinese and disclaimer
    assert "投资建议" in p


# ---------- Session history ----------

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
    # Tool digests go only into the system background record, never the assistant body
    # (once concatenated into the body, the model imitated them and output "[本轮工具调用]" to the user)
    assert "[本轮工具调用]" not in msgs[1]["content"]
    assert "query_financials" not in msgs[1]["content"]
    record = msgs[-1]["content"]
    assert "query_financials" in record and "严禁" in record
    # Undefined session doesn't blow up; empty history returns []
    assert history_store.get_messages(None) == []
    assert history_store.get_messages("no-such") == []


def test_history_no_tool_record_without_tools():
    """Sessions without tool calls get no background-record system message."""
    history_store.reset("t-s4")
    history_store.append_user("t-s4", "什么是市盈率")
    history_store.append_assistant("t-s4", "市盈率 = 市值 / 净利润 …", [])
    msgs = history_store.get_messages("t-s4")
    assert [m["role"] for m in msgs] == ["user", "assistant"]


def test_history_injection_caps():
    """Injection turn cap works (beyond max_turns only the most recent turns are kept)."""
    history_store.reset("t-s2")
    for i in range(10):
        history_store.append_user("t-s2", f"问题{i}" * 10)
        history_store.append_assistant("t-s2", f"回答{i}" * 10, [])
    msgs = history_store.get_messages("t-s2", max_turns=3)
    users = [m["content"] for m in msgs if m["role"] == "user"]
    assert len(users) == 3
    assert "问题9" in users[-1]          # most recent kept
    assert "问题0" not in users[0]       # oldest truncated away


def test_history_ttl_cleanup():
    """Expired sessions are lazily cleaned (simulate 30 minutes ago by editing ts directly)."""
    import time
    history_store.reset("t-s3")
    history_store.append_user("t-s3", "旧会话")
    with history_store._LOCK:
        for t in history_store._STORE["t-s3"]:
            t["ts"] = time.time() - 3600
    history_store.append_user("t-tick", "触发清理")   # triggers lazy cleanup
    assert history_store.get_messages("t-s3") == []


# ---------- Sandbox parameters ----------

def test_sandbox_config():
    assert MAX_ROUNDS <= 10
    assert REPEAT_LIMIT >= 2
    assert TOOL_TIMEOUTS["run_quant_scan"] > DEFAULT_TOOL_TIMEOUT
    assert DEFAULT_TOOL_TIMEOUT >= 60


def test_tool_manifest_read_only():
    """The conversation loop can only see read-only tools (forge_chain invisible)."""
    from harness.mcp.bridge import openai_tools
    tools = openai_tools(include_write=False)
    names = {t["function"]["name"] for t in tools}
    assert "forge_chain" not in names
    assert len(names) == 31   # 31 read-only (including build_company_profile)
    assert "query_financials" in names and "verify_forecasts" in names


# ---------- Table extraction ----------

def test_extract_table_shapes():
    assert _extract_table({"columns": ["a"], "data": [{"a": 1}], "rows": 1})
    assert _extract_table({"table": {"columns": ["a"], "data": [{"a": 1}]}})
    assert _extract_table({"foo": 1}) is None
    assert _extract_table({"data": []}) is None
    assert _extract_table(None) is None


# ---------- Fast-lane NL2SQL ----------

def test_extract_sql_shapes():
    from harness.loop import _extract_sql
    assert _extract_sql("SELECT 1").strip() == "SELECT 1"
    assert _extract_sql("```sql\nSELECT a FROM t WHERE x=1;\n```"
                         ).strip() == "SELECT a FROM t WHERE x=1"
    assert _extract_sql("好的, SQL 如下:\nWITH x AS (SELECT 1) SELECT * FROM x"
                         ).startswith("WITH")
    assert _extract_sql("抱歉我无法生成") is None
    assert _extract_sql("") is None
    assert _extract_sql("UPDATE t SET a=1") is None   # non-SELECT/WITH rejected


def test_fast_lane_llm_fallback_to_rules():
    """LLM generation failure / None result falls back to the rule engine (needs DB, no LLM)."""
    import asyncio
    from harness import loop as loop_mod

    orig = loop_mod._llm_nl2sql
    loop_mod._llm_nl2sql = lambda *a, **k: None    # simulate LLM failure
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


# ---------- Fast-lane gate ----------

def test_fast_path_intent_hit_and_miss():
    from harness.loop import _fast_path_intent
    # Simple query/compare (core keyword high confidence + stock entity) -> fast lane
    assert _fast_path_intent("查询贵州茅台的营收") == "query"
    assert _fast_path_intent("茅台和五粮液谁的毛利率高") == "compare"
    # Compound / other-domain questions -> Agent loop (None)
    assert _fast_path_intent("中际旭创的预期差") is None            # alpha domain
    assert _fast_path_intent("茅台的研报怎么说") is None             # report domain
    assert _fast_path_intent("帮我看看中际旭创有没有财务风险") is None
    assert _fast_path_intent("环比竟然下降了，数据对吗？") is None   # colloquial follow-up
    # No stock entity: common-sense questions / out-of-database targets skip the fast lane
    # ("什么是市盈率" was once force-run as a data query)
    assert _fast_path_intent("什么是市盈率") is None
    assert _fast_path_intent("特斯拉的营收是多少") is None
    assert _fast_path_intent("白酒行业毛利率排名") is None           # industry dimension goes to the Agent
    # Deep-water words: compound intents / extended domains skip the fast lane
    # ("市盈率和估值分位一起看看" was once hijacked and half-answered)
    assert _fast_path_intent("中际旭创的最新市盈率和估值分位一起看看") is None
    assert _fast_path_intent("贵州茅台的营收分析一下") is None
    assert _fast_path_intent("中际旭创的研报怎么说") is None
    assert _fast_path_intent("") is None


# ---------- Parallel execution ----------

def test_execute_batch_parallel_and_ordered():
    """Multiple same-round tool calls must run in parallel (total elapsed ~= max, not sum) with results order-preserved."""
    import asyncio
    import time
    from harness import loop as loop_mod

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
        assert [r["data"] for r in results] == [1, 2, 3]   # order preserved
        assert elapsed < 0.8, f"疑似串行: {elapsed:.2f}s"    # serial would be ~0.9s
    finally:
        loop_mod._run_tool = orig
