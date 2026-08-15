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


# 常用指标中文映射 -> (字段, 数据源类型)
# source: financial=财务三表+指标表 / market=行情+估值表
METRIC_ALIAS_FULL = {
    "营收": {"field": "i.revenue", "source": "financial"},
    "营业收入": {"field": "i.revenue", "source": "financial"},
    "营业总收入": {"field": "i.total_revenue", "source": "financial"},
    "归母净利润": {"field": "i.n_income_attr_p", "source": "financial"},
    "净利润": {"field": "i.n_income_attr_p", "source": "financial"},
    "扣非净利润": {"field": "i.net_after_nr_lp_correct", "source": "financial"},
    "营业利润": {"field": "i.operate_profit", "source": "financial"},
    "利润总额": {"field": "i.total_profit", "source": "financial"},
    "每股收益": {"field": "i.basic_eps", "source": "financial"},
    "毛利率": {"field": "f.grossprofit_margin", "source": "financial"},
    "净利率": {"field": "f.netprofit_margin", "source": "financial"},
    "净资产收益率": {"field": "f.roe", "source": "financial"},
    "roe": {"field": "f.roe", "source": "financial"},
    "净利润同比增长": {"field": "f.netprofit_yoy", "source": "financial"},
    "营收同比增长": {"field": "f.or_yoy", "source": "financial"},
    "资产负债率": {"field": "f.debt_to_assets", "source": "financial"},
    "流动比率": {"field": "f.current_ratio", "source": "financial"},
    "速动比率": {"field": "f.quick_ratio", "source": "financial"},
    "总资产": {"field": "b.total_assets", "source": "financial"},
    "总负债": {"field": "b.total_liab", "source": "financial"},
    "净资产": {"field": "b.equity_attr_p", "source": "financial"},
    "货币资金": {"field": "b.money_cap", "source": "financial"},
    "应收账款": {"field": "b.accounts_receiv", "source": "financial"},
    "存货": {"field": "b.invent", "source": "financial"},
    "商誉": {"field": "b.goodwill", "source": "financial"},
    "经营现金流": {"field": "c.n_cashflow_act", "source": "financial"},
    "投资现金流": {"field": "c.n_cashflow_inv_act", "source": "financial"},
    "筹资现金流": {"field": "c.n_cash_flows_fnc_act", "source": "financial"},
    "市盈率": {"field": "db.pe_ttm", "source": "market"},
    "市净率": {"field": "db.pb", "source": "market"},
    "总市值": {"field": "db.total_mv", "source": "market"},
    "市值": {"field": "db.total_mv", "source": "market"},
    "流通市值": {"field": "db.circ_mv", "source": "market"},
    "换手率": {"field": "db.turnover_rate", "source": "market"},
    "换手": {"field": "db.turnover_rate", "source": "market"},
    "收盘价": {"field": "d.close", "source": "market"},
    "股价": {"field": "d.close", "source": "market"},
    "涨跌幅": {"field": "d.pct_chg", "source": "market"},
}

# 兼容旧引用
METRIC_ALIAS = {
    k: v["field"] for k, v in METRIC_ALIAS_FULL.items()
}

# 关键词映射 (通用, 命中即用)
TABLE_FIELD_MAP = {
    "营收": ("i", "revenue", "financial"),
    "收入": ("i", "revenue", "financial"),
    "净利": ("i", "n_income_attr_p", "financial"),
    "利润": ("i", "operate_profit", "financial"),
    "毛利": ("f", "grossprofit_margin", "financial"),
    "roe": ("f", "roe", "financial"),
    "资产": ("b", "total_assets", "financial"),
    "负债": ("b", "total_liab", "financial"),
    "市值": ("db", "total_mv", "market"),
    "市盈": ("db", "pe_ttm", "market"),
    "市净": ("db", "pb", "market"),
    "现金流": ("c", "n_cashflow_act", "financial"),
    "股价": ("d", "close", "market"),
    "收盘": ("d", "close", "market"),
    "涨跌幅": ("d", "pct_chg", "market"),
    "换手": ("db", "turnover_rate", "market"),
}


# ============================================================
# 规则降级 SQL 生成 (无 LLM 时使用)
# ============================================================

def _rule_based_sql(user_input: str) -> Optional[str]:
    """
    规则模板 SQL 生成: 只支持预设查询模式
    流程: 先识别指标名 -> 逆行提取股票名 -> 按指标类型选数据源
    """
    text = user_input.strip()

    # 0. 预处理: 截断"和/与"连接的第二个指标 (规则只支持单指标)
    #    仅当"和/与"出现在指标之后 (双指标场景), 保留第一个
    first_metric_clean = None

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

    # 2. 提取股票名 (指标之前的内容, 去掉动作词/年份/时间词/"的")
    prefix = text[:metric_pos]
    # 完整动词优先, 避免 '查一下' 被 '查' 先吃掉
    prefix = re.sub(
        r"(?:查一下|查一查|查询|帮我查一下|帮我查|请问|看看|查看|请查|帮我|请|查|看|"
        r"的|、|和|与|最近|最新|近|\\s+)",
        "", prefix,
    )
    # 去掉年份和时间范围 (2020年 / 2020 年 / 最近两年 / 前三年)
    prefix = re.sub(r"\d{4}\s*年", "", prefix)
    prefix = re.sub(r"最近|最近一年|最近两年|最近三年|近一年|近两年|近三年|今年以来", "", prefix)
    # 去掉开头残留空词
    prefix = re.sub(r"^(查询结果|请|帮我|一下)", "", prefix).strip()
    if not prefix:
        return None
    stock = prefix
    ts_code = lookup_ts_code(stock)
    if not ts_code:
        return None

    field, source = _match_metric_field(matched_metric)
    if not field:
        return None

    # 3. 按数据源生成 SQL
    if source == "market":
        # 行情类: daily + daily_basic (最新交易日)
        return f"""SELECT d.trade_date, s.name AS stock_name, d.close, d.open, d.high, d.low,
       d.vol, d.amount, d.pct_chg, db.pe_ttm, db.pb, db.total_mv, db.circ_mv
FROM stock.daily d
JOIN stock.stock_basic s ON d.ts_code = s.ts_code
LEFT JOIN stock.daily_basic db
    ON d.trade_date = db.trade_date AND d.ts_code = db.ts_code
WHERE d.ts_code = '{ts_code}'
ORDER BY d.trade_date DESC
LIMIT 8"""

    # 财务类: income + fina_indicator (合并报表)
    return f"""SELECT i.ts_code, s.name AS stock_name, i.end_date, {field}
FROM fin.income i
JOIN stock.stock_basic s ON i.ts_code = s.ts_code
LEFT JOIN fin.fina_indicator f
    ON i.ts_code = f.ts_code AND i.end_date = f.end_date AND i.report_type = f.report_type
WHERE i.ts_code = '{ts_code}' AND i.report_type = '1'
ORDER BY i.end_date DESC
LIMIT 8"""


def _match_metric_field(metric: str) -> tuple[Optional[str], str]:
    """
    将指标名称映射到 (数据库字段, 数据源类型)
    source: 'financial' (财务三表+指标) / 'market' (行情+估值)
    """
    m = metric.lower()
    # 精确别名匹配 (带数据源标记)
    for key, entry in METRIC_ALIAS_FULL.items():
        if m == key.lower() or m in key.lower():
            return entry["field"], entry["source"]

    # 关键词映射
    for key, (prefix, field, source) in TABLE_FIELD_MAP.items():
        if key in m:
            return f"{prefix}.{field}", source
    return None, "financial"


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
        """规则优先 (词典+模板, 零成本), LLM 兜底"""
        # 1. 规则引擎 (高频固定句式, 无需 LLM)
        try:
            from skills.fin_query.rule_engine import get_rule_engine
            sql, meta = get_rule_engine().parse(user_input)
            if sql:
                return sql, f"规则查询 (类型={meta.get('type')})", []
        except Exception as e:
            logger.warning(f"规则引擎异常: {e}")

        # 2. LLM 兜底
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
                logger.warning(f"LLM SQL 生成失败: {e}")

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