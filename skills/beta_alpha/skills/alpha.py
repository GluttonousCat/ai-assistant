"""Compose the four Alpha quadrants and generate their research summary."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple


from core.logger import get_logger
from harness.llm import get_agent_llm
from skills.base import BaseSkill, SkillContext
from tools.finance import data as finance_data
from skills.beta_alpha.analysis.alpha_context import (
    to_yuan,
    classify_drift,
    edge_trend,
    forecast_divergence,
    financial_momentum,
    valuation_context,
    report_views,
)
from skills.beta_alpha.prompts import ALPHA_SUMMARY_PROMPT

logger = get_logger(__name__)


def _resolve_stock(text: str) -> Optional[Tuple[str, str]]:
    """Returns (name, ts_code); prefers the stock dictionary (match_stock
    returns (ts_code, alias)), then tries a 6-digit code"""
    code: Optional[str] = None
    try:
        from tools.finance.stock_kb import get_stock_kb

        kb = get_stock_kb()
        kb.load()
        hit = kb.match_stock(text)
        if hit:
            code = hit[0]
    except Exception as e:
        logger.warning(f"股票词典匹配失败: {e}")
    if not code:
        m = re.search(r"(\d{6})\.(SZ|SH|BJ)", text.upper())
        if m:
            code = f"{m.group(1)}.{m.group(2)}"
    if not code:
        return None
    name = finance_data.stock_name(code)
    return (name, code) if name else None


class AlphaSkill(BaseSkill):
    """Single-stock expectation-gap mining: forecast divergence x financial momentum x valuation percentile x report views"""

    name = "alpha"
    description = (
        "个股预期差分析: 券商盈利预测分歧度/调整方向、财务边际动量、"
        "估值分位、研报观点四象限综合解读"
    )
    intent_keywords = ["预期差", "分歧", "拐点", "alpha"]

    def __init__(self, llm: Optional[Any] = None):
        self.llm = llm or get_agent_llm()

    def analyze(self, name: str, ts_code: str) -> Dict:
        forecast_rows, momentum_rows, valuation_rows, view_rows = (
            finance_data.alpha_inputs(ts_code, name)
        )
        forecast = forecast_divergence(forecast_rows)
        momentum = financial_momentum(momentum_rows)
        valuation = valuation_context(valuation_rows)
        views = report_views(view_rows)
        return {
            "ts_code": ts_code,
            "name": name,
            "forecast": forecast,
            "momentum": momentum,
            "valuation": valuation,
            "views": views,
        }

    def build_summary_prompt(self, res: Dict) -> str:
        return ALPHA_SUMMARY_PROMPT.format(
            stock_name=res["name"],
            ts_code=res["ts_code"],
            forecast=json.dumps(res["forecast"], ensure_ascii=False, default=str),
            momentum=json.dumps(res["momentum"], ensure_ascii=False, default=str),
            valuation=json.dumps(res["valuation"], ensure_ascii=False, default=str),
            views=json.dumps(res["views"], ensure_ascii=False, default=str),
        )

    @staticmethod
    def build_table(res: Dict) -> Tuple[List[str], List[Dict]]:
        """Four-quadrant result -> flat table for the frontend data event (shared by run and SSE)"""
        f = res["forecast"][0] if res["forecast"] else None
        m = res["momentum"][-1] if res["momentum"] else {}
        tr = m.get("trend") or {}
        pe, pb = res["valuation"].get("pe_ttm"), res["valuation"].get("pb")
        cols = ["metric", "value", "signal"]
        data = [
            {
                "metric": "预测覆盖",
                "value": f"{f['n_forecasts']}份/{f['n_orgs']}家" if f else "无覆盖",
                "signal": f["forecast_period"] if f else "—",
            },
            {
                "metric": "预测分歧度CV",
                "value": f["cv"] if f and f.get("cv") is not None else "—",
                "signal": (
                    "分歧大"
                    if f and f.get("cv") and f["cv"] > 0.3
                    else ("分歧小" if f and f.get("cv") is not None else "—")
                ),
            },
            {
                "metric": "预测方向",
                "value": f["direction"] or "—" if f else "—",
                "signal": f"{f['drift_pct']}%"
                if f and f.get("drift_pct") is not None
                else "—",
            },
            {
                "metric": "营收同比(累计)",
                "value": m.get("or_yoy"),
                "signal": tr.get("or_yoy_trend") or "—",
            },
            {
                "metric": "单季营收同比",
                "value": m.get("q_sales_yoy"),
                "signal": tr.get("q_sales_yoy_trend") or "—",
            },
            {
                "metric": "净利同比(累计)",
                "value": m.get("netprofit_yoy"),
                "signal": tr.get("netprofit_yoy_trend") or "—",
            },
            {
                "metric": "毛利率",
                "value": m.get("grossprofit_margin"),
                "signal": tr.get("gm_trend") or "—",
            },
            {
                "metric": "PE(TTM)分位",
                "value": pe["pct_rank"] if pe else None,
                "signal": f"现值{pe['latest']}" if pe else "数据不足",
            },
            {
                "metric": "PB分位",
                "value": pb["pct_rank"] if pb else None,
                "signal": f"现值{pb['latest']}" if pb else "数据不足",
            },
        ]
        return cols, data

    def run(self, context: SkillContext) -> SkillContext:
        hit = _resolve_stock(context.user_input)
        if not hit:
            context.error = (
                "未识别到股票。请带上股票名或代码, "
                "例如: 中际旭创的预期差 / 300308 的分歧度"
            )
            return context
        name, ts_code = hit
        try:
            res = self.analyze(name, ts_code)
        except Exception as e:
            logger.error(f"AlphaSkill 分析失败: {e}")
            context.error = f"预期差分析失败: {e}"
            return context

        cols, data = self.build_table(res)
        context.result = {
            "ts_code": ts_code,
            "name": name,
            "quadrants": res,
            "data": data,
            "columns": cols,
            "rows": len(data),
        }
        try:
            context.result["summary"] = self.llm.invoke(self.build_summary_prompt(res))
        except Exception as e:
            logger.warning(f"AlphaSkill 综述生成失败: {e}")
            context.result["summary"] = "四象限数据已生成, 但综述生成失败。"
        return context


if __name__ == "__main__":
    import sys

    text = sys.argv[1] if len(sys.argv) > 1 else "中际旭创的预期差"
    ctx = AlphaSkill()(SkillContext(user_input=text))
    if ctx.error:
        print("ERROR:", ctx.error)
    else:
        import pandas as pd

        print(
            pd.DataFrame(ctx.result["data"], columns=ctx.result["columns"]).to_string(
                index=False
            )
        )
        print("\n---- 综述 ----\n", ctx.result.get("summary", "")[:800])

__all__ = ["AlphaSkill", "to_yuan", "classify_drift", "edge_trend"]
