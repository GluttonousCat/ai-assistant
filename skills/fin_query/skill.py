"""
Financial query skill (Text-to-SQL)
Pipeline: schema linking -> SQL generation -> safety validation -> execution -> result interpretation

Design notes:
- Use rule templates first, then fall back to the LLM when configured
- Safety first: every SQL must pass sql_guard validation
- Resolve stock names with the stock knowledge base
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from core.config import get_config
from core.json_response import parse_json_response
from core.logger import get_logger
from tools.finance.metrics import QUERY_METRICS as METRIC_ALIAS_FULL
from tools.finance.query import execute_readonly, lookup_ts_code
from tools.finance.sql_guard import validate_sql
from tools.finance.schema_info import get_schema_info
from skills.base import BaseSkill, SkillContext

logger = get_logger(__name__)


# Legacy imports expose the original query labels and field strings.
METRIC_ALIAS = {label: metric["field"] for label, metric in METRIC_ALIAS_FULL.items()}


class FinQuerySkill(BaseSkill):
    name = "fin_query"
    description = "自然语言查询财务与行情数据 (Text-to-SQL)"
    intent_keywords = [
        "查询", "查一下", "多少", "营收", "净利润", "roe", "毛利率",
        "排名", "筛选", "大于", "小于", "同比",
    ]

    def __init__(self):
        self.config = get_config()
        self.schema_info = get_schema_info()
        self._llm = None

    @property
    def llm(self):
        """Lazily load the LLM client"""
        if self._llm is None and self.config.openai_api_key:
            from harness.llm import LLMClient
            self._llm = LLMClient()
        return self._llm

    # ---------- Main entry ----------
    def run(self, context: SkillContext) -> SkillContext:
        user_input = context.user_input
        context.log(f"FinQuerySkill 开始处理: {user_input[:50]}")
        context_stocks = (context.params or {}).get("context_stocks") or []

        # 1. Generate SQL
        sql, explanation, tables = self._generate_sql(user_input, context_stocks)
        if not sql:
            context.error = self._friendly_parse_error(user_input)
            return context
        context.log(f"生成 SQL: {sql}")

        # 2. Safety validation
        guard = validate_sql(sql)
        if not guard.valid:
            context.error = f"SQL 校验失败: {guard.error}"
            context.log(context.error)
            return context

        # 3. Execute
        try:
            result_df, elapsed = self._execute_sql(sql)
        except Exception as e:
            context.error = f"查询执行失败: {e}"
            context.log(context.error)
            return context
        context.log(f"查询返回 {len(result_df)} 行, 耗时 {elapsed:.2f}s")

        # 4. Interpret the result (LLM or rules)
        summary = self._interpret_result(user_input, explanation, result_df)

        context.result = {
            "data": result_df.to_dict("records"),
            "columns": list(result_df.columns),
            "summary": summary,
            "sql": sql,
            "rows": len(result_df),
        }
        context.log(f"完成, {len(result_df)} 行")
        return context

    # ---------- SQL generation ----------
    def _generate_sql(self, user_input: str,
                      context_stocks: Optional[List[Tuple[str, str]]] = None
                      ) -> tuple[Optional[str], str, List[str]]:
        """Rules first (dictionary + templates, zero cost), LLM as fallback
        context_stocks: stocks from the previous turn [(ts_code, name)], inherited
        when a follow-up question omits the subject"""
        # 1. Rule engine (high-frequency fixed phrasings, no LLM needed)
        try:
            from skills.fin_query.rule_engine import get_rule_engine
            sql, meta = get_rule_engine().parse(user_input, context_stocks)
            if sql:
                return sql, f"规则查询 (类型={meta.get('type')})", []
        except Exception as e:
            logger.warning(f"规则引擎异常: {e}")

        # 2. LLM fallback (context stocks injected into the prompt to support omitted-subject cases)
        if self.llm:
            try:
                from skills.fin_query.prompts import SQL_GENERATION_PROMPT
                ctx_hint = ""
                if context_stocks:
                    names = "、".join(n for _, n in context_stocks)
                    ctx_hint = f"\n(会话上文提到的股票: {names}, 若用户未指明可沿用)"
                prompt = SQL_GENERATION_PROMPT.format(
                    schema_info=self.schema_info.to_prompt_text(),
                    user_query=user_input + ctx_hint,
                )
                resp = self.llm.invoke(prompt)
                parsed = _extract_json(resp)
                if parsed and parsed.get("sql"):
                    return (
                        parsed["sql"],
                        parsed.get("explanation", ""),
                        parsed.get("tables_used", []),
                    )
            except Exception as e:
                logger.warning(f"LLM SQL 生成失败: {e}")

        return None, "", []

    @staticmethod
    def _friendly_parse_error(user_input: str) -> str:
        """Guided hint on parse failure (distinguish missing stock / missing metric / not understood at all)"""
        try:
            from tools.finance.stock_kb import get_stock_kb
            kb = get_stock_kb()
            has_metric = kb.match_metric(user_input) is not None
            has_stock = kb.match_stock(user_input) is not None
            if has_metric and not has_stock:
                return ("请指定要查询的股票, 例如:\n"
                        "- 查询平安银行的营收\n- 看看贵州茅台的毛利率")
            if has_stock and not has_metric:
                return ("请说明要查询的指标, 例如:\n"
                        "- 茅台的毛利率\n- 宁德时代的营收")
        except Exception:
            pass
        return ("无法解析查询意图。支持的问法示例:\n"
                "- 查询平安银行的营收\n- 看看贵州茅台的毛利率\n"
                "- 对比茅台和五粮液的ROE\n- 白酒板块毛利率最高的5家")

    # ---------- Execution ----------
    def _execute_sql(self, sql: str) -> tuple[pd.DataFrame, float]:
        t0 = time.time()
        # enforce the LIMIT ceiling
        df = execute_readonly(sql)
        return df, time.time() - t0

    # ---------- Result interpretation ----------
    def _interpret_result(self, user_query: str, explanation: str,
                          df: pd.DataFrame) -> str:
        if df is None or df.empty:
            return "查询无结果。可能原因：该报告期无数据、股票代码不正确、或筛选条件过严。"

        # LLM interpretation
        if self.llm:
            try:
                from skills.fin_query.prompts import RESULT_INTERPRET_PROMPT
                prompt = RESULT_INTERPRET_PROMPT.format(
                    user_query=user_query,
                    explanation=explanation,
                    result_table=df.head(20).to_string(),
                    max_rows=20,
                )
                return self.llm.invoke(prompt)
            except Exception as e:
                logger.warning(f"LLM 解读失败: {e}")

        # Rule-based interpretation: brief display
        return (_result_brief(df))


def _result_brief(df: pd.DataFrame) -> str:
    """Rule-based result summary"""
    lines: List[str] = []
    rows = df.head(10)
    cols = [c for c in df.columns if c not in ("ts_code", "trad_date")]
    if "end_date" in df.columns and "stock_name" in df.columns:
        lines.append("查询结果:")
        for _, r in rows.iterrows():
            vals = " | ".join(f"{c}: {_fmt(r[c])}" for c in cols[:5])
            lines.append(f"  {r.get('stock_name','')} {r.get('end_date','')}: {vals}")
    else:
        lines.append("查询结果:")
        for _, r in rows.iterrows():
            vals = " | ".join(f"{c}: {_fmt(r[c])}" for c in df.columns[:6])
            lines.append(f"  {vals}")
    if len(df) > 10:
        lines.append(f"  ... 共 {len(df)} 行, 仅显示前 10 行")
    return "\n".join(lines)


def _fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "-"
    if isinstance(v, float):
        return f"{v:,.2f}"
    return str(v)


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    return parse_json_response(text, fences="all")
