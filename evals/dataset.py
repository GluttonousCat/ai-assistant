"""
Eval dataset structure definitions (v2)

Redesigned against the old generate_cases' deficits in scale/diversity/annotation:

- Data source: bound to real data in the database (reliable expectations + backfillable "ideal expectations")
- Layering: easy / colloquial / hard / adversarial (aligned with PRD F1 difficulty tiers)
- Annotation: gold_slots (stock/metric/time/condition) — prepared for Phase2 NER training
- Verdict: each case carries asset classes + expectation (± data) — evals can judge precisely

Fields:
  id           unique case id (fun_/corp_/compa_/rank_/edge_...)
  question     natural-language input
  intent       expected_intent: query/compare/detect/verify/report/unknown
  complexity   easy/normal/hard/attack (evaluation tier)
  assets       asset classes involved (fin / market / db_missing) — for expectation management
  gold         structured annotation: {stocks:[{name, ts_code}], metrics:[standard metrics], time, conditions:[...]}
  expects      precise expectation for differentiated verdicts: {has_result: true/false/maybe, intent: ...}
  category     dataset block (financial/market/compare/rank/combined/aliases/edge/attack)
  note         case note (optional)

Design principles:
- Precisely expectable (data exists and is queryable) -> has_result: true
- Cannot produce a result due to backfill/data gaps (e.g. daily_basic break-off, or financials of stocks outside the 3 fully-covered ones) -> has_result: maybe (judged by "cleanly returns 0 rows/empty set" rather than requiring data)
- "maybe" cases skip strict data_hit checks but still count for sql/exec/intent verdicts
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# Standard metric identifiers (internal)
# fin_i_: fin.income; fin_f_: fin.fina_indicator; fin_b_: balancesheet; fin_c_: cashflow;
# mkt_d_: stock.daily; mkt_db_: stock.daily_basic
METRIC_STD = {
    # --- Financials (income) ---
    "revenue": "fin_i_revenue",              # operating revenue
    "total_revenue": "fin_i_total_revenue",  # total operating revenue
    "n_income_attr_p": "fin_i_n_income_attr_p",  # net profit attributable to parent
    "n_income": "fin_i_n_income",            # net profit (incl. minority)
    "operate_profit": "fin_i_operate_profit",
    "total_profit": "fin_i_total_profit",
    "basic_eps": "fin_i_basic_eps",
    # --- Financials (fina_indicator) ---
    "roe": "fin_f_roe",
    "netprofit_margin": "fin_f_netprofit_margin",
    "grossprofit_margin": "fin_f_grossprofit_margin",
    "debt_to_assets": "fin_f_debt_to_assets",
    "current_ratio": "fin_f_current_ratio",
    "quick_ratio": "fin_f_quick_ratio",
    "netprofit_yoy": "fin_f_netprofit_yoy",
    "or_yoy": "fin_f_or_yoy",
    # --- Financials (balancesheet) ---
    "total_assets": "fin_b_total_assets",
    "total_liab": "fin_b_total_liab",
    "total_hldr_eqy_exc_min_int": "fin_b_total_hldr_eqy_exc_min_int",
    "goodwill": "fin_b_goodwill",
    # --- Financials (cashflow) ---
    "n_cashflow_act": "fin_c_n_cashflow_act",
    # --- Quotes (daily) ---
    "close": "mkt_d_close",
    "pct_chg": "mkt_d_pct_chg",
    "vol": "mkt_d_vol",
    # --- Quotes (daily_basic, data break-off!) ---
    "pe_ttm": "mkt_db_pe_ttm",
    "pb": "mkt_db_pb",
    "total_mv": "mkt_db_total_mv",
    "circ_mv": "mkt_db_circ_mv",
    "turnover_rate": "mkt_db_turnover_rate",
}
STD_TO_LABEL = {
    "fin_i_revenue": "营业收入",
    "fin_i_total_revenue": "营业总收入",
    "fin_i_n_income_attr_p": "归母净利润",
    "fin_i_n_income": "净利润",
    "fin_i_operate_profit": "营业利润",
    "fin_i_total_profit": "利润总额",
    "fin_i_basic_eps": "每股收益",
    "fin_f_roe": "ROE",
    "fin_f_netprofit_margin": "净利率",
    "fin_f_grossprofit_margin": "毛利率",
    "fin_f_debt_to_assets": "资产负债率",
    "fin_f_current_ratio": "流动比率",
    "fin_f_quick_ratio": "速动比率",
    "fin_f_netprofit_yoy": "净利润同比增速",
    "fin_f_or_yoy": "营收同比增速",
    "fin_b_total_assets": "总资产",
    "fin_b_total_liab": "总负债",
    "fin_b_equity_attr_p": "净资产",
    "fin_b_goodwill": "商誉",
    "fin_c_n_cashflow_act": "经营现金流",
    "mkt_d_close": "收盘价",
    "mkt_d_pct_chg": "涨跌幅",
    "mkt_d_vol": "成交量",
    "mkt_db_pe_ttm": "市盈率",
    "mkt_db_pb": "市净率",
    "mkt_db_total_mv": "总市值",
    "mkt_db_circ_mv": "流通市值",
    "mkt_db_turnover_rate": "换手率",
}

# Data asset classes (for expectation management: all reliable symbols + the 3 full-financials stocks)
FIN3_CODES = {"000001.SZ", "000002.SZ", "000006.SZ"}          # all three financial statements complete
DAILY_OK = True                                               # stock.daily fully available
DB_OK = False                                                 # daily_basic break-off (only to 2011, out of step with daily)

# ---- Eval stock pool (reliability annotated) ----
# Standard stocks fully present in the tables (general purpose)
STOCK_POOL = [
    ("贵州茅台", "600519.SH", "白酒"),
    ("五粮液", "000858.SZ", "白酒"),
    ("泸州老窖", "000568.SZ", "白酒"),
    ("山西汾酒", "600809.SH", "白酒"),
    ("招商银行", "600036.SH", "银行"),
    ("工商银行", "601398.SH", "银行"),
    ("宁波银行", "002142.SZ", "银行"),
    ("中国平安", "601318.SH", "保险"),
    ("宁德时代", "300750.SZ", "电气设备"),
    ("比亚迪", "002594.SZ", "汽车整车"),
    ("格力电器", "000651.SZ", "家用电器"),
    ("美的集团", "000333.SZ", "家用电器"),
    ("生益科技", "600183.SH", "元器件"),
    ("沪电股份", "002463.SZ", "元器件"),
    ("兆易创新", "603986.SH", "半导体"),
    ("北方华创", "002371.SZ", "半导体"),
    ("药明康德", "603259.SH", "化学制药"),
    ("恒瑞医药", "600276.SH", "化学制药"),
    # With all three financial statements (income/bs/cashflow)
    ("平安银行", "000001.SZ", "银行"),
    ("万科A", "000002.SZ", "全国地产"),
    ("深振业A", "000006.SZ", "区域地产"),
]

# Colloquial aliases (real or common, for alias-type cases)
ALIAS_MAP = [
    ("600519.SH", ["茅台", "毛子"]),
    ("000858.SZ", ["五粮"]),
    ("000568.SZ", ["泸州", "老窖"]),
    ("600036.SH", ["招行"]),
    ("601398.SH", ["工行"]),
    ("601318.SH", ["平安", "平保"]),
    ("300750.SZ", ["宁德", "宁王", "曾毓群家"]),
    ("002594.SZ", ["比王", "BYD"]),
    ("000651.SZ", ["格力"]),
    ("600183.SH", ["生益"]),
    ("002463.SZ", ["沪电"]),
    ("603986.SH", ["兆易"]),
    ("002371.SZ", ["北方"]),
    ("002142.SZ", ["波行", "宁波"]),
    ("000001.SZ", ["平银"]),
    ("000002.SZ", ["万科"]),
    ("600809.SH", ["汾酒"]),
]

# The 3 full-financials stocks (income/bs/cashflow present, all financial fields reliable)
FIN3 = [("平安银行", "000001.SZ", "银行"),
        ("万科A", "000002.SZ", "全国地产"),
        ("深振业A", "000006.SZ", "区域地产")]


@dataclass
class EvalCase:
    id: str
    question: str
    intent: str                    # query/compare/detect/verify/report/unknown
    complexity: str                # easy/normal/hard/attack
    category: str                  # financial/market/compare/rank/combined/aliases/edge/attack
    assets: List[str] = field(default_factory=list)   # fin/market/db_missing
    gold: Dict[str, Any] = field(default_factory=dict)
    expects: Dict[str, Any] = field(default_factory=dict)
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "question": self.question,
            "intent": self.intent,
            "complexity": self.complexity,
            "category": self.category,
            "assets": self.assets,
            "gold": self.gold,
            "expects": self.expects,
            "note": self.note,
        }


def mk_case(cid: str, q: str, intent: str, complexity: str, category: str,
            assets: Optional[List[str]] = None,
            gold: Optional[Dict[str, Any]] = None,
            expects: Optional[Dict[str, Any]] = None,
            note: str = "") -> EvalCase:
    return EvalCase(
        id=cid, question=q, intent=intent, complexity=complexity,
        category=category, assets=assets or [], gold=gold or {},
        expects=expects or {"has_result": False}, note=note,
    )