"""Financial metric names used by natural-language rules, prompts and MCP tools."""

# Longest-name matching uses insertion order to resolve equal-length aliases.
RULE_METRICS: dict[str, tuple[str, str]] = {
    # Revenue
    "营业收入": ("revenue", "i"),
    "营收": ("revenue", "i"),
    "收入": ("revenue", "i"),
    "营业总收入": ("total_revenue", "i"),
    "营业额": ("revenue", "i"),
    "卖了多少": ("revenue", "i"),
    # Profit
    "归母净利润": ("n_income_attr_p", "i"),
    "净利润": ("n_income_attr_p", "i"),
    "净利": ("n_income_attr_p", "i"),
    "赚了多少钱": ("n_income_attr_p", "i"),
    "赚了多少": ("n_income_attr_p", "i"),
    "利润是多少": ("n_income_attr_p", "i"),
    "扣非净利润": ("net_after_nr_lp_correct", "i"),
    "营业利润": ("operate_profit", "i"),
    "利润总额": ("total_profit", "i"),
    "每股收益": ("basic_eps", "i"),
    "eps": ("basic_eps", "i"),
    "每股赚多少": ("basic_eps", "i"),
    "稀释每股收益": ("diluted_eps", "i"),
    # Ratios (fina_indicator)
    "roe": ("roe", "f"),
    "净资产收益率": ("roe", "f"),
    "回报率": ("roe", "f"),
    "净利率": ("netprofit_margin", "f"),
    "销售净利率": ("netprofit_margin", "f"),
    "毛利率": ("grossprofit_margin", "f"),
    "销售毛利率": ("grossprofit_margin", "f"),
    "毛利": ("grossprofit_margin", "f"),
    "资产负债率": ("debt_to_assets", "f"),
    "负债率": ("debt_to_assets", "f"),
    "流动比率": ("current_ratio", "f"),
    "速动比率": ("quick_ratio", "f"),
    "净利润同比增长": ("netprofit_yoy", "f"),
    "净利润同比": ("netprofit_yoy", "f"),
    "净利同比": ("netprofit_yoy", "f"),
    "净利增速": ("netprofit_yoy", "f"),
    "营收同比增长": ("or_yoy", "f"),
    "营收同比": ("or_yoy", "f"),
    "营收增速": ("or_yoy", "f"),
    # Assets
    "总资产": ("total_assets", "b"),
    "资产总额": ("total_assets", "b"),
    "总负债": ("total_liab", "b"),
    "负债总额": ("total_liab", "b"),
    "净资产": ("total_hldr_eqy_exc_min_int", "b"),
    "股东权益": ("total_hldr_eqy_exc_min_int", "b"),
    "权益": ("total_hldr_eqy_exc_min_int", "b"),
    "货币资金": ("money_cap", "b"),
    "应收账款": ("accounts_receiv", "b"),
    "存货": ("inventories", "b"),
    "商誉": ("goodwill", "b"),
    # Cash flow
    "经营现金流": ("n_cashflow_act", "c"),
    "经营活动现金流": ("n_cashflow_act", "c"),
    "投资现金流": ("n_cashflow_inv_act", "c"),
    "筹资现金流": ("n_cash_flows_fnc_act", "c"),
    "经营性现金流": ("n_cashflow_act", "c"),
    # Market quotes
    "收盘价": ("close", "d"),
    "股价": ("close", "d"),
    "最新价": ("close", "d"),
    "现价": ("close", "d"),
    "现在多少钱": ("close", "d"),
    "多少钱": ("close", "d"),
    "涨跌幅": ("pct_chg", "d"),
    "涨跌": ("pct_chg", "d"),
    "涨幅": ("pct_chg", "d"),
    "涨了没": ("pct_chg", "d"),
    "涨了多少": ("pct_chg", "d"),
    "成交量": ("vol", "d"),
    "成交额": ("amount", "d"),
    "市盈率": ("pe_ttm", "db"),
    "pe": ("pe_ttm", "db"),
    "市净率": ("pb", "db"),
    "pb": ("pb", "db"),
    "总市值": ("total_mv", "db"),
    "市值": ("total_mv", "db"),
    "流通市值": ("circ_mv", "db"),
    "换手率": ("turnover_rate", "db"),
}

# The public query vocabulary and its prompt order are narrower than the rule parser.
_QUERY_LABELS = (
    "营收", "营业收入", "营业总收入", "归母净利润", "净利润",
    "扣非净利润", "营业利润", "利润总额", "每股收益", "毛利率",
    "净利率", "净资产收益率", "roe", "净利润同比增长", "营收同比增长",
    "资产负债率", "流动比率", "速动比率", "总资产", "总负债",
    "净资产", "货币资金", "应收账款", "存货", "商誉",
    "经营现金流", "投资现金流", "筹资现金流", "市盈率", "市净率",
    "总市值", "市值", "流通市值", "换手率", "换手",
    "收盘价", "股价", "涨跌幅",
)


def _query_metrics():
    metrics = {}
    for label in _QUERY_LABELS:
        field, prefix = RULE_METRICS["换手率" if label == "换手" else label]
        metrics[label] = {
            "field": f"{prefix}.{field}",
            "source": "market" if prefix in ("d", "db") else "financial",
        }
    return metrics


QUERY_METRICS = _query_metrics()
