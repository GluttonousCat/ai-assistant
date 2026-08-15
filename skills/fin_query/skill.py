"""
财务查询 Skill (Text-to-SQL)
链路: schema linking -> SQL 生成 -> 安全校验 -> 执行 -> 结果解读

设计要点:
- LLM 可选: 配置了 OPENAI_API_KEY 用 LLM 生成, 否则降级为规则模板 (保证可验证)
- 安全第一: 所有 SQL 必须通过 sql_guard 校验
- 股票名映射: 通过 stock.stock_basic 反查 ts_code
"""
from __future__ import annotations

import json
import re
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from core.config import get_config
from core.logger import get_logger
from storage.pg import PgClient
from tools.finance.sql_guard import validate_sql, force_limit, SqlGuardResult
from tools.finance.schema_info import get_schema_info
from skills.base import BaseSkill, SkillContext

logger = get_logger(__name__)


# ============================================================
# 股票名称映射与工具
# ============================================================

def lookup_ts_code(name_or_code: str) -> Optional[str]:
    """将股票名称或代码转换为 ts_code (如 '贵州茅台' -> '600519.SH')"""
    name = name_or_code.strip()
    if not name:
        return None

    # 已经是 ts_code 格式 (6位+.SH/.SZ)
    if re.match(r"^\d{6}\.(SH|SZ|BJ)$", name.upper()):
        return name.upper()

    with PgClient() as pg:
        # 精确名称
        row = pg.fetch_one(
            "SELECT ts_code FROM stock.stock_basic WHERE name=%s",
            (name,),
        )
        if row:
            return row["ts_code"]
        # 模糊名称 (名称包含)
        row = pg.fetch_one(
            "SELECT ts_code FROM stock.stock_basic WHERE name LIKE %s LIMIT 1",
            (f"%{name}%",),
        )
        if row:
            return row["ts_code"]
        # 6位代码无后缀
        if re.match(r"^\d{6}$", name):
            row = pg.fetch_one(
                "SELECT ts_code FROM stock.stock_basic WHERE symbol=%s",
                (name,),
            )
            if row:
                return row["ts_code"]
    return None


# 常用指标中文映射 -> 表.字段
METRIC_ALIAS = {
    "营收": "i.revenue",
    "营业收入": "i.revenue",
    "营业总收入": "i.total_revenue",
    "归母净利润": "i.n_income_attr_p",
    "净利润": "i.n_income_attr_p",
    "扣非净利润": "i.net_after_nr_lp_correct",
    "营业利润": "i.operate_profit",
    "利润总额": "i.total_profit",
    "每股收益": "f.basic_eps",
    "毛利率": "f.grossprofit_margin",
    "净利率": "f.netprofit_margin",
    "净资产收益率": "f.roe",
    "roe": "f.roe",
    "净利润同比增长": "f.netprofit_yoy",
    "营收同比增长": "f.or_yoy",
    "资产负债率": "f.debt_to_assets",
    "流动比率": "f.current_ratio",
    "速动比率": "f.quick_ratio",
    "总资产": "b.total_assets",
    "总负债": "b.total_liab",
    "净资产": "b.equity_attr_p",
    "货币资金": "b.money_cap",
    "应收账款": "b.accounts_receiv",
    "存货": "b.invent",
    "商誉": "b.goodwill",
    "经营现金流": "c.n_cashflow_act",
    "投资现金流": "c.n_cashflow_inv_act",
    "筹资现金流": "c.n_cash_flows_fnc_act",
    "市盈率": "v.pe_ttm",
    "市净率": "v.pb",
    "总市值": "v.total_mv",
    "流通市值": "v.circ_mv",
    "换手率": "v.turnover_rate",
    "收盘价": "v.close",
    "涨跌幅": "v.pct_chg",
}


# ============================================================
# 规则降级 SQL 生成 (无 LLM 时使用)
# ============================================================

def _rule_based_sql(user_input: str) -> Optional[str]:
    """
    规则模板 SQL 生成: 只支持预设查询模式
    流程: 先识别指标名 -> 逆行提取股票名
    """
    text = user_input.strip()

    # 1. 找到指标位置 (按指标关键词在文本中搜索)
    metric_pos = None
    matched_metric = None
    for metric_kw in sorted(METRIC_ALIAS.keys(), key=len, reverse=True):
        idx = text.find(metric_kw)
        if idx >= 0:
            metric_pos = idx
            matched_metric = metric_kw
            break

    if metric_pos is None:
        return None

    # 2. 提取股票名 (指标之前的内容, 去掉动作词和"的")
    prefix = text[:metric_pos]
    # 完整动词优先, 避免 '查一下' 被 '查' 先吃掉
    prefix = re.sub(
        r"(?:查一下|查一查|查询|看看|查看|帮我查一下|请查|查|看|的|、|和|与|\\s+)",
        "", prefix,
    )
    # 去掉无意义词
    prefix = re.sub(r"^(查询结果|请|帮我|最近|最新|一下)", "", prefix).strip()
    if not prefix:
        return None
    stock = prefix
    ts_code = lookup_ts_code(stock)
    if not ts_code:
        return None

    field = _match_metric_field(matched_metric)
    if not field:
        return None

    return f"""SELECT i.ts_code, s.name AS stock_name, i.end_date, {field}
FROM fin.income i
JOIN stock.stock_basic s ON i.ts_code = s.ts_code
LEFT JOIN fin.fina_indicator f
    ON i.ts_code = f.ts_code AND i.end_date = f.end_date AND i.report_type = f.report_type
WHERE i.ts_code = '{ts_code}' AND i.report_type = '1'
ORDER BY i.end_date DESC
LIMIT 8"""


def _match_metric_field(metric: str) -> Optional[str]:
    """
    将指标名称映射到数据库字段 (支持 target 表组合)
    """
    m = metric.lower()
    # 优先精确匹配别名
    for key, field in METRIC_ALIAS.items():
        if m == key.lower() or m in key.lower():
            return field

    # 关键词映射 (指标 -> 表前缀 + 字段)
    TABLE_FIELD_MAP = {
        "营收": ("i", "revenue"),
        "收入": ("i", "revenue"),
        "净利": ("i", "n_income_attr_p"),
        "利润": ("i", "operate_profit"),
        "每股收益": ("f", "basic_eps"),
        "毛利": ("f", "grossprofit_margin"),
        "净利率": ("f", "netprofit_margin"),
        "roe": ("f", "roe"),
        "净资产收益": ("f", "roe"),
        "资产": ("b", "total_assets"),
        "负债": ("b", "total_liab"),
        "市值": ("v", "total_mv"),
        "市盈": ("v", "pe_ttm"),
        "市净": ("v", "pb"),
        "现金流": ("c", "n_cashflow_act"),
    }
    for key, (prefix, field) in TABLE_FIELD_MAP.items():
        if key in m:
            return f"{prefix}.{field}"
    return None


# ============================================================
# Skill 实现
# ============================================================

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
        """懒加载 LLM 客户端"""
        if self._llm is None and self.config.openai_api_key:
            from llm.client import LLMClient
            self._llm = LLMClient()
        return self._llm

    # ---------- 主入口 ----------
    def run(self, context: SkillContext) -> SkillContext:
        user_input = context.user_input
        context.log(f"FinQuerySkill 开始处理: {user_input[:50]}")

        # 1. 生成 SQL
        sql, explanation, tables = self._generate_sql(user_input)
        if not sql:
            context.error = "无法解析查询意图"
            return context
        context.log(f"生成 SQL: {sql}")

        # 2. 安全校验
        guard = validate_sql(sql)
        if not guard.valid:
            context.error = f"SQL 校验失败: {guard.error}"
            context.log(context.error)
            return context

        # 3. 执行
        try:
            result_df, elapsed = self._execute_sql(sql)
        except Exception as e:
            context.error = f"查询执行失败: {e}"
            context.log(context.error)
            return context
        context.log(f"查询返回 {len(result_df)} 行, 耗时 {elapsed:.2f}s")

        # 4. 结果解读 (LLM 或规则)
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

    # ---------- SQL 生成 ----------
    def _generate_sql(self, user_input: str) -> tuple[Optional[str], str, List[str]]:
        """LLM 优先, 规则降级"""
        # 尝试 LLM
        if self.llm:
            try:
                from skills.fin_query.prompts import SQL_GENERATION_PROMPT
                prompt = SQL_GENERATION_PROMPT.format(
                    schema_info=self.schema_info.to_prompt_text(),
                    user_query=user_input,
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
                logger.warning(f"LLM SQL 生成失败, 降级规则: {e}")

        # 规则降级
        sql = _rule_based_sql(user_input)
        if sql:
            return sql, "基于规则模板生成的查询", ["fin.income", "fin.fina_indicator", "stock.stock_basic"]
        return None, "", []

    # ---------- 执行 ----------
    def _execute_sql(self, sql: str) -> tuple[pd.DataFrame, float]:
        t0 = time.time()
        # 强制 LIMIT 上限
        sql = force_limit(sql, max_limit=1000)
        with PgClient() as pg:
            df = pg.fetch_df(sql)
        return df, time.time() - t0

    # ---------- 结果解读 ----------
    def _interpret_result(self, user_query: str, explanation: str,
                          df: pd.DataFrame) -> str:
        if df is None or df.empty:
            return "查询无结果。可能原因：该报告期无数据、股票代码不正确、或筛选条件过严。"

        # LLM 解读
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

        # 规则解读: 简要展示
        return (_result_brief(df))


def _result_brief(df: pd.DataFrame) -> str:
    """规则化结果摘要"""
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
    """从 LLM 输出提取 JSON 对象"""
    if not text:
        return None
    # 去除 ```json ... ``` 包裹
    text = re.sub(r"```(?:json)?|```", "", text).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # 提取第一个 {...}
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None