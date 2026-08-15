"""
数据字典生成器
从 PostgreSQL 读取真实表结构, 生成 LLM prompt 使用的 schema 描述
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional

from storage.pg import PgClient

# 表 -> 字段说明 (用于 prompt, 补充数据库层面无注释的情况)
FIELD_DESCRIPTIONS: Dict[str, Dict[str, str]] = {
    "income": {
        "total_revenue": "营业总收入",
        "revenue": "营业收入",
        "n_income": "净利润(含少数股东)",
        "n_income_attr_p": "净利润(归母)",
        "operate_profit": "营业利润",
        "total_profit": "利润总额",
        "basic_eps": "基本每股收益",
        "rd_exp": "研发费用",
        "ebit": "息税前利润",
        "ebitda": "息税折旧摊销前利润",
    },
    "balancesheet": {
        "total_assets": "总资产",
        "total_liab": "总负债",
        "equity_attr_p": "净资产(归母)",
        "money_cap": "货币资金",
        "accounts_receiv": "应收账款",
        "invent": "存货",
        "goodwill": "商誉",
        "total_share": "总股本",
        "free_share": "自由流通股本",
    },
    "cashflow": {
        "n_cashflow_act": "经营活动现金流净额",
        "n_cashflow_inv_act": "投资活动现金流净额",
        "n_cash_flows_fnc_act": "筹资活动现金流净额",
    },
    "fina_indicator": {
        "roe": "净资产收益率",
        "roe_waa": "加权平均净资产收益率",
        "netprofit_yoy": "净利润同比增长率",
        "or_yoy": "营业收入同比增长率",
        "grossprofit_margin": "毛利率",
        "netprofit_margin": "净利率",
        "debt_to_assets": "资产负债率",
        "current_ratio": "流动比率",
        "quick_ratio": "速动比率",
        "assets_turn": "总资产周转率",
        "inv_turn": "存货周转率",
    },
    "daily": {
        "open": "开盘价", "high": "最高价", "low": "最低价", "close": "收盘价",
        "vol": "成交量(手)", "amount": "成交额(千元)", "pct_chg": "涨跌幅(%)",
    },
    "daily_basic": {
        "pe_ttm": "市盈率(TTM)", "pb": "市净率", "ps_ttm": "市销率(TTM)",
        "turnover_rate": "换手率(%)", "total_mv": "总市值(万元)",
        "circ_mv": "流通市值(万元)", "dv_ratio": "股息率(%)",
    },
}


# 表 -> 中文名 + 简要说明
TABLE_INTRO = {
    "income": "利润表(按报告期, 单位:元)。columns: ts_code,end_date,report_type,...",
    "balancesheet": "资产负债表(按报告期, 单位:元)。columns: ts_code,end_date,...",
    "cashflow": "现金流量表(按报告期, 单位:元)",
    "fina_indicator": "财务指标(按报告期)。含ROE/增长率/毛利率/偿债指标",
    "daily": "日K行情(按交易日×股票)",
    "daily_basic": "每日估值指标(PE/PB/市值/换手)",
    "stock_basic": "股票基础信息(代码/名称/行业/板块)",
    "adj_factor": "复权因子(后复权)",
    "trade_calendar": "交易日历(是否交易日)",
    "v_financial_summary": "财务宽表(三表+指标join) 推荐查询用",
    "v_daily_valuation": "日频行情+估值宽表 推荐查询用",
}


class SchemaInfo:
    """数据字典"""

    def __init__(self):
        self._cache: Optional[str] = None
        self._columns_cache: Dict[str, List[str]] = {}

    def _load_columns(self) -> Dict[str, List[str]]:
        """从 PG 读取各表列名"""
        if self._columns_cache:
            return self._columns_cache

        with PgClient() as pg:
            for schema in ("stock", "fin"):
                tables = pg.fetch_all(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema=%s AND table_type='BASE TABLE' "
                    "ORDER BY table_name",
                    (schema,),
                )
                for t in tables:
                    tn = t["table_name"]
                    cols = pg.fetch_all(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name=%s "
                        "ORDER BY ordinal_position",
                        (schema, tn),
                    )
                    self._columns_cache[tn] = [c["column_name"] for c in cols]

            # 视图
            for schema in ("stock", "fin"):
                views = pg.fetch_all(
                    "SELECT table_name FROM information_schema.views "
                    "WHERE table_schema=%s ORDER BY table_name",
                    (schema,),
                )
                for v in views:
                    vn = v["table_name"]
                    cols = pg.fetch_all(
                        "SELECT column_name FROM information_schema.columns "
                        "WHERE table_schema=%s AND table_name=%s "
                        "ORDER BY ordinal_position",
                        (schema, vn),
                    )
                    self._columns_cache[vn] = [c["column_name"] for c in cols]

        return self._columns_cache

    def to_prompt_text(self, tables: Optional[List[str]] = None) -> str:
        """生成 LLM prompt 用的 schema 描述文本"""
        cols = self._load_columns()
        lines: List[str] = []

        for name in ("v_financial_summary", "v_daily_valuation", "fina_indicator",
                     "income", "balancesheet", "cashflow", "daily_basic", "daily",
                     "stock_basic"):
            if tables and name not in tables:
                continue
            if name not in cols:
                continue

            intro = TABLE_INTRO.get(name, name)
            lines.append(f"表 {name}: {intro}")
            lines.append(f"  字段: {', '.join(cols[name][:40])}")
            lines.append(f"  关键字段说明:")
            descs = FIELD_DESCRIPTIONS.get(name, {})
            if descs:
                for f, d in descs.items():
                    if f in cols[name]:
                        lines.append(f"    - {f}: {d}")
            lines.append("")

        return "\n".join(lines)

    def to_json(self) -> str:
        """JSON 格式 (供程序使用)"""
        cols = self._load_columns()
        return json.dumps(cols, ensure_ascii=False, indent=2)


# 单例
_schema_info: Optional[SchemaInfo] = None


def get_schema_info() -> SchemaInfo:
    global _schema_info
    if _schema_info is None:
        _schema_info = SchemaInfo()
    return _schema_info


if __name__ == "__main__":
    print(get_schema_info().to_prompt_text())