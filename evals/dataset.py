"""
评测数据集结构定义 (v2)

针对旧 generate_cases 的量级/多样性/标注缺陷重新设计:

- 数据来源: 与数据库真实数据绑定 (可靠预期 + 可回填的"理想预期")
- 分层: 简单 / 口语 / 复杂 / 对抗 (对齐 PRD F1 难易分级)
- 标注: gold_slots (股票/指标/时间/条件) — 为 Phase2 NER 训练做准备
- 判定: 每个用例给出 资产类别 + 预期 (±数据) — 评测可精确判定

字段:
  id           用例唯一标识 (fun_/corp_/compa_/rank_/edge_...)
  question     自然语言输入
  intent       expected_intent: query/compare/detect/verify/report/unknown
  complexity   easy/normal/hard/attack (评估分层)
  assets       涉及的资产类别 (fin / market / db_missing) — 用于预期管理
  gold         结构化标注: {stocks:[{name, ts_code}], metrics:[标准指标], time, conditions:[...]}
  expects      区分判定的精确预期: {has_result: true/false/maybe, intent: ...}
  category     数据集区块 (financial/market/compare/rank/combined/aliases/edge/attack)
  note         用例说明 (可选)

设计原则:
- 能精确预期的 (data 存在且可查询) -> has_result: true
- 因回填/数据缺失无法产生结果的 (如 daily_basic 断档, 或非3股财务) -> has_result: maybe (评测按"是否正常返回0行或空集"而非必有数据判)
- maybe 类不参与 data_hit 严格判定, 但参与 sql/exec/意图判定
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

# 标准指标标识 (内部)
# fin_i_: fin.income; fin_f_: fin.fina_indicator; fin_b_: balancesheet; fin_c_: cashflow;
# mkt_d_: stock.daily; mkt_db_: stock.daily_basic
METRIC_STD = {
    # --- 财务 (income) ---
    "revenue": "fin_i_revenue",              # 营业收入
    "total_revenue": "fin_i_total_revenue",  # 营业总收入
    "n_income_attr_p": "fin_i_n_income_attr_p",  # 归母净利润
    "n_income": "fin_i_n_income",            # 净利润(含少数)
    "operate_profit": "fin_i_operate_profit",
    "total_profit": "fin_i_total_profit",
    "basic_eps": "fin_i_basic_eps",
    # --- 财务 (fina_indicator) ---
    "roe": "fin_f_roe",
    "netprofit_margin": "fin_f_netprofit_margin",
    "grossprofit_margin": "fin_f_grossprofit_margin",
    "debt_to_assets": "fin_f_debt_to_assets",
    "current_ratio": "fin_f_current_ratio",
    "quick_ratio": "fin_f_quick_ratio",
    "netprofit_yoy": "fin_f_netprofit_yoy",
    "or_yoy": "fin_f_or_yoy",
    # --- 财务 (balancesheet) ---
    "total_assets": "fin_b_total_assets",
    "total_liab": "fin_b_total_liab",
    "equity_attr_p": "fin_b_equity_attr_p",
    "goodwill": "fin_b_goodwill",
    # --- 财务 (cashflow) ---
    "n_cashflow_act": "fin_c_n_cashflow_act",
    # --- 行情 (daily) ---
    "close": "mkt_d_close",
    "pct_chg": "mkt_d_pct_chg",
    "vol": "mkt_d_vol",
    # --- 行情 (daily_basic, 数据断档!) ---
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

# 数据资产类别 (用于预期管理: 全部可靠标的 + 财务3股)
FIN3_CODES = {"000001.SZ", "000002.SZ", "000006.SZ"}          # 财务三表齐全
DAILY_OK = True                                               # daily 全量可用
DB_OK = False                                                 # daily_basic 断档 (只到2011, 与daily错开)

# ---- 评测用股票池 (标注可靠程度) ----
# 全量在表中的标准股票 (通用)
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
    # 财务三表齐全的 (income/bs/cashflow)
    ("平安银行", "000001.SZ", "银行"),
    ("万科A", "000002.SZ", "全国地产"),
    ("深振业A", "000006.SZ", "区域地产"),
]

# 口语别名 (真实存在或常见, 用于别名类用例)
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

# 财务3股 (income/bs/cashflow 存在, 财务全字段可靠)
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