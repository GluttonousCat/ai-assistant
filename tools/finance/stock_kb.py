"""
股票知识库 (Stock KB)
提供:
- 别名词典加载 (PG stock_basic + stock_alias -> 内存词典)
- 最长匹配实体提取 (股票/指标/时间)
- 词典查找接口

设计:
- 词典从 PG 一次性加载到内存 (5000+ 条目, 可忽略内存)
- 提取用"最长匹配"而非正则: 命中多个同义别名时取最长最具体
- 纯规则, 无 LLM 依赖, CPU 微秒级
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from storage.pg import PgClient

# 字符串匹配常量
MAX_NAME_LEN = 12  # 股票别名最长长度 (防误匹配)

# 时间表达解析 (仅做结构化; 具体 SQL 由规则引擎组)
# 返回: (kind, value)  kind: year/range/recent, value: int/日期对/None
def parse_time_phrase(text: str) -> Optional[Tuple[str, object]]:
    """解析时间词: 2019年/2023年/最近两年/近三年/去年"""
    text = text.strip()
    m = re.search(r"(19|20)\d{2}\s*年", text)
    if m:
        return ("year", int(m.group(0).strip()[:4]))
    m = re.search(r"最近(\d{1,2})年|近(\d{1,2})年|近(\d{1,2})年", text)
    if m:
        n = int(next(g for g in m.groups() if g))
        return ("recent_years", n)
    if "去年" in text:
        return ("previous_year", None)
    if "今年" in text:
        return ("this_year", None)
    if "最近" in text or "最新" in text:
        return ("recent", None)
    return None


class StockKB:
    """股票知识库 (单例)"""

    _instance: Optional["StockKB"] = None

    def __init__(self):
        # alias(小写) -> (ts_code, 标准名)
        self._stock_alias: Dict[str, str] = {}
        # ts_code -> 标准名 (为 SQL 里的 stock_name)
        self._code_to_name: Dict[str, str] = {}
        self._loaded = False

    def load(self, force: bool = False) -> None:
        """从 PG 加载股票基础 + 别名"""
        if self._loaded and not force:
            return
        self._stock_alias = {}
        self._code_to_name = {}
        try:
            with PgClient() as pg:
                # 基础表: 标准名 -> 代码 (所有在市股票)
                rows = pg.fetch_all(
                    "SELECT ts_code, name FROM stock.stock_basic"
                )
                for r in rows:
                    self._code_to_name[r["ts_code"]] = r["name"]
                    self._stock_alias[r["name"].lower()] = r["ts_code"]
                    # 6位纯代码也算
                    sym = r["ts_code"].split(".")[0]
                    self._stock_alias[sym] = r["ts_code"]

                # 别名词典
                rows = pg.fetch_all("SELECT alias, ts_code FROM stock.stock_alias")
                for r in rows:
                    self._stock_alias[r["alias"].lower()] = r["ts_code"]
        except Exception as e:
            # 表不存在时降级为空
            import logging
            logging.getLogger(__name__).warning(f"StockKB 加载失败: {e}")
        self._loaded = True

    # ---------- 股票匹配 ----------
    def match_stock(self, text: str) -> Optional[Tuple[str, str]]:
        """
        在文本中查找最长匹配的股票别名
        返回 (ts_code, 匹配文本) 或 None
        """
        self.load()
        text_l = text.lower()
        best = None  # (length, ts_code, matched)

        # 先全量扫描表内所有 alias 名做子串匹配
        for alias, code in self._stock_alias.items():
            if len(alias) > MAX_NAME_LEN:
                continue
            if alias in text_l:
                if best is None or len(alias) > best[0]:
                    best = (len(alias), code, alias)
        if best:
            return best[1], best[2]
        return None

    def match_stock_exact(self, name: str) -> Optional[str]:
        """精确匹配 (词典直接命中)"""
        self.load()
        return self._stock_alias.get(name.strip().lower())

    def get_name(self, ts_code: str) -> Optional[str]:
        self.load()
        return self._code_to_name.get(ts_code)

    # ---------- 指标匹配 ----------
    # 指标别名 -> (标准字段, 数据源表前缀)
    # 复用 skills.fin_query 的映射避免重复
    METRIC_ALIASES: Dict[str, Tuple[str, str]] = {
        # 收入类
        "营业收入": ("revenue", "i"),
        "营收": ("revenue", "i"),
        "收入": ("revenue", "i"),
        "营业总收入": ("total_revenue", "i"),
        # 利润类
        "归母净利润": ("n_income_attr_p", "i"),
        "净利润": ("n_income_attr_p", "i"),
        "净利": ("n_income_attr_p", "i"),
        "扣非净利润": ("net_after_nr_lp_correct", "i"),
        "营业利润": ("operate_profit", "i"),
        "利润总额": ("total_profit", "i"),
        "每股收益": ("basic_eps", "i"),
        "稀释每股收益": ("diluted_eps", "i"),
        # 指标类 (fina_indicator)
        "roe": ("roe", "f"),
        "净资产收益率": ("roe", "f"),
        "净利率": ("netprofit_margin", "f"),
        "销售净利率": ("netprofit_margin", "f"),
        "毛利率": ("grossprofit_margin", "f"),
        "销售毛利率": ("grossprofit_margin", "f"),
        "资产负债率": ("debt_to_assets", "f"),
        "流动比率": ("current_ratio", "f"),
        "速动比率": ("quick_ratio", "f"),
        "净利润同比增长": ("netprofit_yoy", "f"),
        "净利同比": ("netprofit_yoy", "f"),
        "营收同比增长": ("or_yoy", "f"),
        "营收同比": ("or_yoy", "f"),
        # 资产类
        "总资产": ("total_assets", "b"),
        "总负债": ("total_liab", "b"),
        "净资产": ("equity_attr_p", "b"),
        "货币资金": ("money_cap", "b"),
        "应收账款": ("accounts_receiv", "b"),
        "存货": ("invent", "b"),
        "商誉": ("goodwill", "b"),
        # 现金类
        "经营现金流": ("n_cashflow_act", "c"),
        "经营活动现金流": ("n_cashflow_act", "c"),
        "投资现金流": ("n_cashflow_inv_act", "c"),
        "筹资现金流": ("n_cash_flows_fnc_act", "c"),
        "经营性现金流": ("n_cashflow_act", "c"),
        # 行情类
        "收盘价": ("close", "d"),
        "股价": ("close", "d"),
        "最新价": ("close", "d"),
        "涨跌幅": ("pct_chg", "d"),
        "市盈率": ("pe_ttm", "db"),
        "pe": ("pe_ttm", "db"),
        "市净率": ("pb", "db"),
        "pb": ("pb", "db"),
        "总市值": ("total_mv", "db"),
        "市值": ("total_mv", "db"),
        "流通市值": ("circ_mv", "db"),
        "换手率": ("turnover_rate", "db"),
    }

    def match_metric(self, text: str) -> Optional[Tuple[str, str]]:
        """
        在文本中查找最长匹配的指标别名
        返回 (标准字段, 表前缀) 或 None
        """
        text_l = text.lower()
        best = None
        for alias, (field, prefix) in self.METRIC_ALIASES.items():
            if alias.lower() in text_l:
                if best is None or len(alias) > best[0]:
                    best = (len(alias), field, prefix)
        if best:
            return best[1], best[2]
        return None

    def all_metric_aliases(self) -> List[str]:
        return list(self.METRIC_ALIASES.keys())


# 单例
_kb: Optional[StockKB] = None


def get_stock_kb() -> StockKB:
    global _kb
    if _kb is None:
        _kb = StockKB()
    return _kb


if __name__ == "__main__":
    kb = get_stock_kb()
    kb.load()
    print(f"词典条目: {len(kb._stock_alias)} 别名")
    for t in ["生益2023年毛利率", "兆易的营收", "招行最近三年净利率", "茅台市值"]:
        print(f"  '{t}' -> stock={kb.match_stock(t)}, metric={kb.match_metric(t)}, time={parse_time_phrase(t)}")