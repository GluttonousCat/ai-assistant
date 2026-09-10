# -*- encoding: utf-8 -*-
"""
Agent 对话循环 (新对话入口的核心)

范式: LLM 自主决策 + MCP 工具循环 (替代旧的 意图路由+固定管道)
  user -> [system(口径知识+守则) + 会话历史 + user]
       -> LLM(deepseek, function-calling, 18 个只读工具)
       -> 工具执行 (线程+超时) -> 结果回填 -> 再决策 ... -> 最终中文回答

沙盒策略 (只读安全):
- 只暴露 read_only=True 工具 (17 个; forge_chain 等写类不进对话)
- 每工具超时 (默认 120s, 重扫描 900s); 超时即返回错误给 LLM, 不再等待
- LLM 决策轮数上限 (MAX_ROUNDS); 重复调用相同工具+参数防死循环
- 工具结果注入 LLM 前截断 (MAX_RESULT_CHARS); 工具本身有行数上限 (df_payload)

事件协议 (SSE, 与旧链路同构, 前端零改动可用):
  stage     {stage:"tool", message:"调用 query_financials …"}   工具执行进度
  tool_call {tool, args, ok, elapsed_ms, rows}                   工具结果元信息 (新)
  data      {intent:"agent", tool, data, columns, rows}          表格型结果 (图表渲染)
  delta     {text}                                               最终回答分块
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, AsyncIterator, Callable, Dict, List, Optional

from core.logger import get_logger
from agent import history_store

logger = get_logger(__name__)

SseFormatter = Callable[[str, object], str]

# ---------- 沙盒参数 ----------
MAX_ROUNDS = 8                       # LLM 决策轮数上限
MAX_RESULT_CHARS = 4000              # 工具结果注入 LLM 的字符上限
REPEAT_LIMIT = 3                     # 相同 (工具,参数) 最多调用次数
MAX_SAME_TOOL_CALLS = 4              # 同名工具 (任意参数) 最多调用次数, 防按环节逐个刷
TOOL_TIMEOUTS: Dict[str, int] = {    # 秒; 未列出的默认 120
    "run_quant_scan": 900,           # 全市场扫描分钟级
    "extract_document": 600,         # 扫描件 OCR
    "read_report": 180,              # 带 question 时内部走 LLM
    "analyze_chain": 300,
}
DEFAULT_TOOL_TIMEOUT = 120


# ---------- 系统提示词 (专业 Know-How 都在这里) ----------
def build_system_prompt() -> str:
    from datetime import date
    return f"""你是 Alpha Finance Radar 平台的投研助手 (今天是 {date.today().isoformat()})。
平台有 A 股行情/财务三表/估值/主营构成/知识星球研报/产业链/量化扫描 全套数据, 你通过工具取数后回答。

【数据纪律 — 最高优先级】
1. 平台内的任何数字 (行情/财务/研报/预测) 必须来自工具返回, 严禁凭记忆报数; 没查到就明说没数据
2. 通用金融概念/常识/方法论可直接讲解, 但要说明"这是通用知识, 非平台数据"
3. 工具返回 ok=false 时, 把原因告诉用户并换正确参数重试一次; 仍失败就如实说明

【财报口径 — "环比/数据对吗"类质疑的关键】
财报各报告期是**累计口径**: 一季报=Q1累计, 中报=上半年累计, 三季报=前三季累计, 年报=全年。
用户说"环比下降"时:
- 若比较的是相邻报告期累计值 (如中报 vs 一季报), 必须先换算单季再下结论:
  Q2单季=中报-Q1, Q3单季=三季报-中报, Q4单季=年报-三季报
- 先用工具重新取数核验 (多取几期), 再回答"数据是对的, 口径是…"——用户质疑数据时,
  禁止只凭上一轮回答辩护, 必须重新查证
- 数据源是 Tushare 官方披露口径, 若确实异常 (如公告修正/口径变更) 如实说明

【多轮对话】
用户追问常省略主语 ("它呢"/"环比呢"/"那家呢")——从会话历史推断上一轮的股票/指标/链条;
工具摘要里记录了上轮查了什么。推断不了就先用 resolve_stock 确认, 再反问用户。

【工具使用】
- 股票类工具**直接传股票名或代码** (工具内部自己解析, 不要先调 resolve_stock);
  resolve_stock 只用于: 问公司身份/行业档案、一句话里多只股票批量解析、别名拿不准时
- 查数值 query_financials (单股/对比/行业/全市场4模式); 估值历史水位 valuation_percentile;
  主营构成 query_main_business
- 券商预期与分歧 get_forecasts; 历史预测兑现校验 verify_forecasts; 预测+动量+估值四象限
  综合判断 analyze_alpha (只要单一维度就用上面对应工具); 财务排雷 detect_financial_risk
- 研报: 先 search_reports 检索清单, 需要某篇细节/单篇问答 read_report;
  只有用户提供的本地文件才用 extract_document (库内研报不用它)
- 产业链 analyze_chain (清单 list_chains); 形态/区间状态 run_quant_scan(regime);
  技术指标数值 (ADX/POC/Wyckoff) compute_indicators
- 一次回答通常 1-3 个工具即可; 全市场扫描 (run_quant_scan 的 range/trend/setup) 很慢,
  用户明确要才用

【表达】
- 一律简体中文; 先直接回答, 再展开依据; 引用具体数值 (带单位: 亿/万/%)
- 绝不向用户输出工具调用记录/执行日志/"[本轮工具调用]"之类的过程信息 —
  工具过程界面已在步骤卡展示, 回答里只放面向用户的结论与依据
- 结尾不做投资建议, 可给"后续观察点"
- 表格数据用户能看到的就不再罗列流水账, 聚焦结论/趋势/口径/风险"""


def _extract_table(data: Any) -> Optional[Dict[str, Any]]:
    """从工具结果中提取表格 (query_financials 顶层; chain/alpha 在 table 键)"""
    if not isinstance(data, dict):
        return None
    for holder in (data, data.get("table")):
        if isinstance(holder, dict) and isinstance(holder.get("data"), list) \
                and isinstance(holder.get("columns"), list) and holder["data"]:
            return {"columns": holder["columns"], "data": holder["data"],
                    "rows": holder.get("rows", len(holder["data"]))}
    return None


def _fmt_tool_args(args: Dict[str, Any]) -> str:
    """参数 -> 短摘要 (stage 气泡与历史记录共用)"""
    parts = []
    for k, v in (args or {}).items():
        if v in (None, "", []):
            continue
        s = str(v)
        parts.append(f"{k}={s[:20]}{'…' if len(s) > 20 else ''}")
    return ", ".join(parts)[:90]


async def _run_tool(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """沙盒执行: 线程 + 超时; 超时后线程仍在后台跑完 (Python 线程不可杀), 但不再阻塞"""
    from mcp import get_registry
    registry = get_registry()
    timeout = TOOL_TIMEOUTS.get(name, DEFAULT_TOOL_TIMEOUT)
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(registry.call, name, args), timeout=timeout)
    except asyncio.TimeoutError:
        return {"ok": False, "tool": name,
                "error": f"工具执行超时 (> {timeout}s)。"
                         f"若是全市场扫描类工具, 建议缩小范围或稍后再试"}


async def _execute_batch(calls: List[tuple]) -> List[Dict[str, Any]]:
    """并行执行一组同轮工具调用 (彼此无依赖), 返回与入参同序的信封列表"""
    if len(calls) == 1:
        return [await _run_tool(calls[0][0], calls[0][1])]
    return list(await asyncio.gather(*[_run_tool(n, a) for n, a in calls]))


# ============================================================
# 快车道: 高置信简单 query/compare 直连 FinQuerySkill (省 1-2 轮 LLM 决策)
# ============================================================

FAST_PATH_CONFIDENCE = 0.85   # 仅核心词命中 (0.9/0.95) 才走, 宁可漏走快车道不可误走
NL2SQL_TIMEOUT = 30           # 快车道 LLM 生成 SQL 超时 (秒), 超时走规则兜底

# 深水区词: 出现任一即放弃快车道 — 这些词意味着复合意图/延伸域/要分析,
# 快车道只会答一半 (如"最新市盈率和估值分位一起看看"曾只返回行情宽表)
_DEEP_WORDS = ("分析", "怎么看", "怎么样", "为什么", "一起", "同时", "还有", "再看看",
               "对比分析", "分位", "水位", "预期差", "分歧", "研报", "风险", "排雷",
               "产业链", "环节", "受益", "形态", "扫描", "预测", "兑现", "财报体检")


def _fast_path_intent(text: str) -> Optional[str]:
    """L1 纯规则判定 (不调 LLM): 返回 'query'/'compare' 走快车道, None 走 Agent 循环

    三重门槛 (宁漏勿误 — 误走快车道会把复合/常识问题截胡答一半):
    ① L1 命中 query/compare 且置信度 >= 0.85 (核心词/组合信号);
    ② 文本含股票实体 (词典/6位代码) — 数据查询必有标的,
      "什么是市盈率"/"特斯拉的营收" 这类无 A 股实体的问题交给 Agent 循环判断;
    ③ 不含深水区词 (_DEEP_WORDS) — 复合意图/延伸域交给 Agent 循环完整作答。
    """
    try:
        if any(w in text for w in _DEEP_WORDS):
            return None
        from agent.intent import get_intent_classifier
        clf = get_intent_classifier()
        r = clf._rule_classify(text)          # L1 only, 毫秒级
        if r.intent not in ("query", "compare") or r.confidence < FAST_PATH_CONFIDENCE:
            return None
        from tools.finance.stock_kb import get_stock_kb
        if not get_stock_kb().match_stock(text):
            return None
        return r.intent
    except Exception as e:  # noqa: BLE001 快车道失败不影响主路径
        logger.debug(f"快车道判定异常: {e}")
    return None


# ============================================================
# 快车道 NL2SQL: LLM 生成 (复杂时间/指标表述更稳), 规则引擎兜底
# ============================================================

def _extract_sql(text: str) -> Optional[str]:
    """从 LLM 输出剥出 SQL (剥 ```sql 围栏, 取第一条 SELECT/WITH 语句)"""
    import re
    if not text:
        return None
    t = text.strip()
    m = re.search(r"```(?:sql)?\s*(.+?)```", t, re.DOTALL | re.IGNORECASE)
    if m:
        t = m.group(1).strip()
    m = re.search(r"((?:SELECT|WITH)\b.+?;?)$", t,
                  re.DOTALL | re.IGNORECASE)
    if not m:
        return None
    sql = m.group(1).strip().rstrip(";")
    return sql if sql.upper().startswith(("SELECT", "WITH")) else None


def _alias_digest(limit: int = 34) -> str:
    """指标中文别名 -> 字段 映射摘要 (进 prompt, 控制长度)"""
    try:
        from agent.skills.fin_query.skill import METRIC_ALIAS_FULL
        items = list(METRIC_ALIAS_FULL.items())[:limit]
        return "; ".join(f"{k}={v['field']}" for k, v in items)
    except Exception:  # noqa: BLE001
        return ""


def _llm_nl2sql(text: str, context_stocks=None) -> Optional[str]:
    """LLM 生成只读 SELECT (同步, 由上层 to_thread 包裹)。

    规则引擎对 '最近三年' 这类相对时间不生成过滤 (recent_years 落空分支),
    时序跨度失真 — LLM 生成对时间/指标/复合条件更稳, 规则引擎仅作兜底。
    """
    import re
    from core.llm.client import get_nl2sql_llm
    from mcp.tools._common import resolve_stocks
    from tools.finance.sql_guard import validate_sql

    # 实体解析在规则层完成 (毫秒级), LLM 只负责拼 SQL — 实体零幻觉
    entities = resolve_stocks(text) or []
    if context_stocks and not entities:
        entities = [{"ts_code": c, "name": n} for c, n in context_stocks]
    ent_desc = "; ".join(f"{e['name']}(ts_code='{e['ts_code']}')" for e in entities) \
        or "无 (全市场/行业查询)"

    prompt = f"""你是 PostgreSQL 只读查询生成器。把用户问题转成**一条** SELECT 语句。

可用表 (只允许 stock/fin 两个 schema, 只读):
- 财务四表联查 (报告期口径): FROM fin.income i JOIN stock.stock_basic s ON i.ts_code=s.ts_code
  LEFT JOIN fin.fina_indicator f ON i.ts_code=f.ts_code AND i.end_date=f.end_date AND i.report_type=f.report_type
  LEFT JOIN fin.balancesheet b ON (同上关联条件) LEFT JOIN fin.cashflow c ON (同上关联条件)
  WHERE i.report_type='1'
  常用字段: i.end_date 报告期, i.revenue 营业收入, i.n_income_attr_p 归母净利润,
  i.basic_eps, i.total_revenue; f.grossprofit_margin 毛利率, f.roe, f.or_yoy, f.netprofit_yoy,
  f.debt_to_assets; b.total_assets, b.goodwill; c.n_cashflow_act 经营现金流
- 行情估值: stock.daily d (trade_date, close, pct_chg) JOIN stock.daily_basic db
  (pe_ttm, pb, total_mv, turnover_rate) ON 同 trade_date+ts_code
- 行业: stock.v_industry_current (ts_code, industry_l1, industry_l2)
- 指标别名: {_alias_digest()}

规则:
1. 只输出 SQL 本身, 不要任何解释或围栏
2. 只读 SELECT; 必须带 LIMIT (时序 ≤48, 其他 ≤30)
3. 时间口径 (财报为累计口径):
   - "最近N年" = 最近 N 个**年报** (end_date 为 12-31 的行): 子查询按 end_date DESC
     取年报 LIMIT N, 输出 ORDER BY end_date ASC — 严禁无限定取 48 期混入季度;
     仅当用户明说"季度/报告期"才取全部报告期
   - "2024年报" = i.end_date = '2024-12-31'; "2024中报" = '2024-06-30';
     "2024" 无后缀 = 该年年报; 行情时间用 d.trade_date
4. 股票实体一律用上面给出的 ts_code 等值过滤; 已解析实体: {ent_desc}
5. 时序查询 ORDER BY end_date ASC (旧→新, 便于图表); 非时序按数值 DESC
6. SELECT 列固定包含 s.name AS stock_name 与报告期 i.end_date
   (行情查询用 d.trade_date) — 即使单期查询也不要省略, 前端展示依赖;
   指标列可用中文别名 (如 i.revenue AS 营业收入)

用户问题: {text}"""
    try:
        resp = get_nl2sql_llm().invoke(prompt)
    except Exception as e:  # noqa: BLE001 LLM 失败走规则兜底
        logger.warning(f"快车道 LLM 生成 SQL 失败: {e}")
        return None
    sql = _extract_sql(resp)
    if sql and validate_sql(sql).valid:
        return sql
    logger.warning(f"快车道 LLM SQL 未过校验, 丢弃: {(sql or '')[:120]}")
    return None


async def _fast_query_stream(text: str, session_id: Optional[str],
                             sse: SseFormatter) -> AsyncIterator[str]:
    """快车道: LLM 生成 SQL (规则引擎兜底) -> 校验 -> 执行 -> 图表 + 规则简报。

    单次 LLM 调用 (~4-10s), 省掉 Agent 循环的工具选择与作答轮;
    简报仍为零 LLM 规则摘要, 深度解读由追问触发 Agent 循环。
    与 Agent 循环同事件协议; 结果写入会话历史, 追问可无缝衔接。
    """
    from agent.context_store import get_session_stocks, update_session_from_result
    from agent.skills.fin_query.skill import FinQuerySkill, _result_brief
    from tools.finance.sql_guard import validate_sql, force_limit

    yield sse("stage", {"stage": "intent", "message": "意图识别: query (快车道)"})
    history_store.append_user(session_id, text)
    skill = FinQuerySkill()
    context_stocks = get_session_stocks(session_id)

    # 1. LLM 生成 (主, 30s 超时), 规则引擎 (兜底)
    yield sse("stage", {"stage": "sql", "message": "LLM 生成查询语句…"})
    sql = None
    try:
        sql = await asyncio.wait_for(
            asyncio.to_thread(_llm_nl2sql, text, context_stocks),
            timeout=NL2SQL_TIMEOUT)
    except asyncio.TimeoutError:
        logger.warning(f"快车道 LLM NL2SQL 超时 (>{NL2SQL_TIMEOUT}s), 走规则兜底")
    except Exception as e:  # noqa: BLE001
        logger.warning(f"快车道 LLM NL2SQL 异常: {e}")
    if not sql:
        sql, _, _tables = await asyncio.to_thread(
            skill._generate_sql, text, context_stocks)
    if not sql:
        yield sse("delta", {"text": skill._friendly_parse_error(text)})
        return
    yield sse("stage", {"stage": "sql", "message": "SQL 已生成, 校验执行中…"})

    guard = validate_sql(sql)
    if not guard.valid:
        yield sse("delta", {"text": f"SQL 校验失败: {guard.error}"})
        return
    try:
        df, elapsed = await asyncio.to_thread(skill._execute_sql, force_limit(sql, 1000))
    except Exception as e:  # noqa: BLE001
        yield sse("delta", {"text": f"查询执行失败: {e}"})
        return

    records = df.to_dict("records")
    update_session_from_result(session_id, text, records)
    yield sse("stage", {"stage": "exec",
                        "message": f"查询返回 {len(df)} 行 ({elapsed:.1f}s)"})
    yield sse("data", {"intent": "query", "tool": "query_financials",
                       "data": records, "columns": list(df.columns),
                       "sql": sql, "rows": len(df)})

    if df is None or df.empty:
        answer = ("查询无结果。可能原因：该报告期无数据、股票代码不正确、或筛选条件过严。"
                  "换一种问法或直接问我怎么查。")
    else:
        answer = (_result_brief(df)
                  + "\n\n(数据已图示。要趋势解读、原因分析或对比, 继续追问即可)")
    yield sse("delta", {"text": answer})

    history_store.append_assistant(
        session_id, answer,
        [{"name": "query_financials", "args": {"直连": text[:40]},
          "ok": True, "elapsed_ms": int(elapsed * 1000), "rows": len(df)}])


async def _decision_loop(llm, tools, messages: List[Dict[str, Any]],
                         sse: SseFormatter, holder: Dict[str, Any],
                         max_rounds: int = MAX_ROUNDS) -> AsyncIterator[str]:
    """共享决策循环 (全工具/域子Agent/升级续跑 共用一份机制)。

    产出 SSE 事件; 结束时写 holder:
      final_text  最终回答 (无工具调用轮的 content)
      tool_events [{name, args, ok, elapsed_ms, rows}] (跨调用累积)
      escalated   bool — 子Agent调用了 escalate 伪工具, 申请全工具接管
    messages 原地追加 (升级续跑时已获上下文保留)。

    沙盒: 只执行本次 manifest 里列出的工具 (模型偶尔会幻觉调用清单外
    但全局注册表里存在的工具名 — 曾实测发生, 必须在执行层拦截并引导走 escalate)。
    """
    from agent.subagent import ESCALATE_TOOL
    allowed = {t["function"]["name"] for t in tools}
    call_counts: Dict[str, int] = {}          # (工具, 参数) 精确防重复
    name_counts: Dict[str, int] = {}          # 同名工具次数 (防按环节逐个刷)
    holder.setdefault("tool_events", [])
    holder["final_text"] = ""
    holder["escalated"] = False

    for round_no in range(1, max_rounds + 1):
        msg = await asyncio.to_thread(llm.invoke_with_tools, messages, tools)
        tool_calls = getattr(msg, "tool_calls", None)
        if not tool_calls:
            holder["final_text"] = (msg.content or "").strip()
            return

        # ---- 解析本轮调用 (escalate 伪工具优先拦截) ----
        batch = []          # [(tc, fname, fargs, env_or_None)]  None=待执行
        for tc in tool_calls:
            fname = tc.function.name
            try:
                fargs = json.loads(tc.function.arguments or "{}")
                if not isinstance(fargs, dict):
                    fargs = {}
            except json.JSONDecodeError:
                fargs = {}

            if fname == ESCALATE_TOOL:
                reason = str(fargs.get("reason", ""))[:100]
                holder["escalated"] = True
                holder["escalate_reason"] = reason
                holder["tool_events"].append({
                    "name": ESCALATE_TOOL, "args": fargs, "ok": True,
                    "elapsed_ms": 0, "rows": 0})
                # 协议完整性: tool_call 必须有对应 tool 消息
                messages.append({"role": "assistant", "content": None,
                                 "tool_calls": [
                                     {"id": tc.id, "type": "function",
                                      "function": {"name": fname,
                                                   "arguments": tc.function.arguments}}]})
                messages.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": "已升级到全工具 Agent, 域限制解除。"})
                yield sse("stage", {"stage": "escalate",
                                    "message": f"子Agent申请全工具接管: {reason}"})
                yield sse("tool_call", {"tool": ESCALATE_TOOL, "args": fargs,
                                        "ok": True, "elapsed_ms": 0, "rows": 0})
                return

            if fname not in allowed:
                # 幻觉工具名: 清单外 (哪怕全局注册表存在) 一律拒绝并引导升级
                batch.append((tc, fname, fargs, {
                    "ok": False, "tool": fname,
                    "error": f"当前子Agent未开放工具 {fname}。"
                             f"若确需该能力请调用 {ESCALATE_TOOL} 说明原因; "
                             f"否则用本域工具完成回答"}))
                continue
            name_counts[fname] = name_counts.get(fname, 0) + 1
            if name_counts[fname] > MAX_SAME_TOOL_CALLS:
                batch.append((tc, fname, fargs, {
                    "ok": False, "tool": fname,
                    "error": f"工具 {fname} 已调用 {name_counts[fname]} 次, "
                             f"请停止继续分解调用, 基于已有结果直接作答"}))
                continue
            batch.append((tc, fname, fargs, None))

        # ---- stage 事件 + 重复调用防护 ----
        for i, (tc, fname, fargs, _) in enumerate(batch):
            key = f"{fname}|{json.dumps(fargs, ensure_ascii=False, sort_keys=True)}"
            call_counts[key] = call_counts.get(key, 0) + 1
            yield sse("stage", {"stage": "tool",
                                "message": f"调用 {fname}({_fmt_tool_args(fargs)})…"})
            if call_counts[key] >= REPEAT_LIMIT:
                batch[i] = (tc, fname, fargs, {
                    "ok": False, "tool": fname,
                    "error": "该调用已重复执行多次且参数相同, 请勿再重复; "
                             "请基于已有结果直接回答用户"})

        # ---- 并行执行 (耗时取 max 而非求和) ----
        pending = [i for i, (_, _, _, env) in enumerate(batch) if env is None]
        if pending:
            results = await _execute_batch(
                [(batch[i][1], batch[i][2]) for i in pending])
            for i, env in zip(pending, results):
                batch[i] = (batch[i][0], batch[i][1], batch[i][2], env)

        # ---- 按原序回填: messages + 事件 ----
        for tc, fname, fargs, env in batch:
            rows = 0
            table = _extract_table(env.get("data")) if env.get("ok") else None
            if table:
                rows = table["rows"]

            holder["tool_events"].append({
                "name": fname, "args": fargs, "ok": env.get("ok", False),
                "elapsed_ms": env.get("elapsed_ms", 0), "rows": rows})

            # 工具结果回填给 LLM (截断保护)
            result_json = json.dumps(env, ensure_ascii=False, default=str)
            if len(result_json) > MAX_RESULT_CHARS:
                result_json = result_json[:MAX_RESULT_CHARS] + \
                    '…(截断, 完整结果已在前端展示)'
            messages.append({"role": "assistant", "content": None,
                             "tool_calls": [
                                 {"id": tc.id, "type": "function",
                                  "function": {"name": fname,
                                               "arguments": tc.function.arguments}}]})
            messages.append({"role": "tool", "tool_call_id": tc.id,
                             "content": result_json})

            yield sse("tool_call", {
                "tool": fname, "args": fargs,
                "ok": env.get("ok", False),
                "elapsed_ms": env.get("elapsed_ms", 0), "rows": rows})
            if table:
                yield sse("data", {"intent": "agent", "tool": fname,
                                   "data": table["data"],
                                   "columns": table["columns"],
                                   "rows": rows})

        if round_no == max_rounds:
            # 轮数用尽: 强制收口 (禁工具, 基于已获数据作答)
            messages.append({"role": "system", "content":
                             "已达工具调用轮数上限, 请立即基于已有信息给出最终中文回答。"})
            msg = await asyncio.to_thread(
                llm.invoke_with_tools, messages, tools, tool_choice="none")
            holder["final_text"] = (msg.content or "").strip()


async def run_agent_stream(text: str, session_id: Optional[str],
                           sse: SseFormatter,
                           fast_lane: bool = True,
                           use_subagent: bool = True) -> AsyncIterator[str]:
    """Agent 主循环 (async generator, 产出 SSE 帧; 不发 done, 由 api 层统一收尾)

    路径: 快车道(高置信简单查询, 零 LLM) → 域子Agent(L1 规则单域命中, 域内
    2~4 工具 + escalate 升级伪工具) → 全工具循环(未命中/复合/升级续跑)。
    fast_lane=False / use_subagent=False 供评测隔离。
    """
    if fast_lane and _fast_path_intent(text):
        async for chunk in _fast_query_stream(text, session_id, sse):
            yield chunk
        return

    from core.llm.client import get_agent_llm
    from mcp.bridge import openai_tools
    from agent.subagent import DOMAIN_DEFS, domain_manifest, route_domain

    llm = get_agent_llm()
    full_tools = openai_tools(include_write=False)   # 沙盒: 只读 17 个
    messages: List[Dict[str, Any]] = [{"role": "system",
                                       "content": build_system_prompt()}]
    messages.extend(history_store.get_messages(session_id))
    messages.append({"role": "user", "content": text})
    history_store.append_user(session_id, text)

    holder: Dict[str, Any] = {}
    domain = route_domain(text) if use_subagent else None

    try:
        if domain:
            d = DOMAIN_DEFS[domain]
            dtools = domain_manifest(domain)
            yield sse("stage", {"stage": "agent",
                                "message": f"意图路由: {d['label']}子Agent "
                                           f"({len(dtools) - 1} 个域内工具)"})
            messages.append({"role": "system", "content": d["prompt"]})
            async for ev in _decision_loop(llm, dtools, messages, sse, holder):
                yield ev
            if holder.get("escalated"):
                yield sse("stage", {"stage": "agent",
                                    "message": "升级: 全工具 Agent 接管…"})
                messages.append({"role": "system", "content":
                                 "域子Agent已申请升级, 全部只读工具现已开放, "
                                 "请基于已有结果继续完成任务并给出最终中文回答。"})
                async for ev in _decision_loop(llm, full_tools, messages,
                                               sse, holder):
                    yield ev
        else:
            yield sse("stage", {"stage": "agent", "message": "Agent 接管, 分析问题…"})
            async for ev in _decision_loop(llm, full_tools, messages, sse, holder):
                yield ev

        final_text = holder.get("final_text") or ""
        tool_events = holder.get("tool_events") or []
        if not final_text:
            final_text = ("抱歉, 本轮未能生成有效回答 (模型输出为空)。"
                          "请换一种问法或稍后重试。")

        history_store.append_assistant(session_id, final_text, tool_events)

        # 最终回答分块推送 (打字机效果; 决策轮为非流式, 此处按块下发)
        for i in range(0, len(final_text), 80):
            yield sse("delta", {"text": final_text[i:i + 80]})
            await asyncio.sleep(0.02)

    except Exception as e:  # noqa: BLE001 顶层兜底, 保证 error 事件一定下发
        logger.error(f"Agent 循环异常: {e}", exc_info=True)
        history_store.append_assistant(
            session_id, f"[异常] {e}", holder.get("tool_events") or [])
        yield sse("error", {"message": f"Agent 执行失败: {e}"})


# ---------- 一次性调用 (非流式, 调试/测试用) ----------
def run_agent(text: str, session_id: Optional[str] = None,
              fast_lane: bool = True) -> Dict[str, Any]:
    """同步入口: 收集全部事件, 返回 {answer, tool_events}"""
    events: List[Dict[str, Any]] = []

    def _collect(event: str, data: Any) -> str:
        events.append({"event": event, **(data if isinstance(data, dict) else {})})
        return ""

    async def _run():
        async for _ in run_agent_stream(text, session_id, _collect, fast_lane=fast_lane):
            pass

    asyncio.run(_run())
    answer = "".join(e.get("text", "") for e in events if e["event"] == "delta")
    # 快车道不产生 tool_call 事件 (只有 data), 从 data 事件补记工具事实
    tool_events = [e for e in events if e["event"] == "tool_call"]
    if not tool_events:
        tool_events = [{"tool": e.get("tool", "query_financials"), "ok": True,
                        "rows": e.get("rows", 0), "elapsed_ms": 0}
                       for e in events if e["event"] == "data"]
    return {"answer": answer,
            "tool_events": tool_events,
            "error": next((e.get("message") for e in events
                           if e["event"] == "error"), None)}


if __name__ == "__main__":
    import sys
    t0 = time.time()
    q = sys.argv[1] if len(sys.argv) > 1 else "中际旭创最新的净利润怎么样"
    sid = sys.argv[2] if len(sys.argv) > 2 else "cli-debug"
    res = run_agent(q, session_id=sid)
    print(f"--- tools: {[ (t['tool'], t['ok']) for t in res['tool_events'] ]}")
    print(f"--- answer ({time.time()-t0:.1f}s):\n{res['answer']}")
    if res["error"]:
        print("ERROR:", res["error"])
