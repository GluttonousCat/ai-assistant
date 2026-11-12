"""
Stock knowledge base (Stock KB)
Provides:
- Alias dictionary loading (PG stock_basic + stock_alias -> in-memory dict)
- Longest-match entity extraction (stock/metric/time)
- Dictionary lookup interfaces

Design:
- Dictionaries are loaded from PG into memory once (5000+ entries, negligible memory)
- Extraction uses "longest match" instead of regex: when multiple synonymous
  aliases hit, take the longest and most specific
- Pure rules, no LLM dependency, microseconds of CPU
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from storage.pg import PgClient

# String matching constants
MAX_NAME_LEN = 12  # Max stock alias length (guards against false matches)


def _strip_roman_suffix(name: str) -> str:
    """Strip the trailing Roman numeral from an industry name (e.g. 'Baijiu II'
    variants -> 'Baijiu')"""
    for suf in ("Ⅲ", "Ⅱ", "ⅰ", "ⅱ", "III", "II", "ⅲ"):
        name = name.replace(suf, "")
    return name.strip()

# Time expression parsing (structuring only; the actual SQL is assembled by
# the rules engine)
# Returns: (kind, value)  kind: year/quarter/half/recent_years/previous_year/this_year/recent
def parse_time_phrase(text: str) -> Optional[Tuple[str, object]]:
    """Parse time phrases: bare year, annual report, quarterly report
    (2023Q1-style or Chinese-numeral style), half-year report, last N years,
    last year. Quarterly patterns are matched before plain years (avoids a
    "2023 Q1 report" being truncated into the 2023 annual report)"""
    text = text.strip()
    # Quarterly report forms: 2023Q1 / year+Q1 / Chinese-numeral or digit
    # quarter+report forms (exact variants in the regex below)
    m = re.search(
        r"(19|20)\d{2}\s*年?\s*[Qq]?\s*([一二三四1234])\s*季报?", text)
    if m:
        q = {"一": 1, "二": 2, "三": 3, "四": 4}[m.group(2)]
        year = int(m.group(0)[:4])
        if q == 4:
            return ("year", year)  # Q4 report = annual report
        return ("quarter", (year, q))
    # Interim/half-year report: 2023H1 and Chinese interim-report forms
    # (exact variants in the regex below)
    m = re.search(r"(19|20)\d{2}\s*年?\s*(?:半年报|中报|H1|h1)", text)
    if m:
        return ("half", int(m.group(0)[:4]))
    # Annual report / bare year
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
    if "最近" in text or "最新" in text or "最近一期" in text:
        return ("recent", None)
    return None


class StockKB:
    """Stock knowledge base (singleton)"""

    _instance: Optional["StockKB"] = None

    def __init__(self):
        # alias (lowercase) -> ts_code
        self._stock_alias: Dict[str, str] = {}
        # ts_code -> canonical name (for stock_name in SQL)
        self._code_to_name: Dict[str, str] = {}
        # industry name (lowercase) -> (index_code, level)
        self._industry_dict: Dict[str, Tuple[str, str]] = {}
        self._loaded = False

    def load(self, force: bool = False) -> None:
        """Load stock basics + aliases from PG"""
        if self._loaded and not force:
            return
        self._stock_alias = {}
        self._code_to_name = {}
        self._industry_dict = {}
        try:
            with PgClient() as pg:
                # Base table: canonical name -> code (all listed stocks)
                rows = pg.fetch_all(
                    "SELECT ts_code, name FROM stock.stock_basic"
                )
                for r in rows:
                    self._code_to_name[r["ts_code"]] = r["name"]
                    self._stock_alias[r["name"].lower()] = r["ts_code"]
                    # Bare 6-digit symbol counts too
                    sym = r["ts_code"].split(".")[0]
                    self._stock_alias[sym] = r["ts_code"]

                # Alias dictionary
                rows = pg.fetch_all("SELECT alias, ts_code FROM stock.stock_alias")
                for r in rows:
                    self._stock_alias[r["alias"].lower()] = r["ts_code"]

                # Industry classification (L1/L2)
                rows = pg.fetch_all(
                    "SELECT index_code, industry_name, level FROM stock.index_classify "
                    "WHERE level IN ('L1','L2')"
                )
                for r in rows:
                    self._industry_dict[r["industry_name"].lower()] = (
                        r["index_code"], r["level"]
                    )
        except Exception as e:
            # Degrade to empty when tables don't exist
            from core.logger import get_logger; get_logger(__name__).warning(f"StockKB 加载失败: {e}")
        self._loaded = True

    # ---------- Stock matching ----------
    def match_stock(self, text: str) -> Optional[Tuple[str, str]]:
        """
        Find the longest-matching stock alias in the text
        Returns (ts_code, matched text) or None
        """
        self.load()
        text_l = text.lower()
        best = None  # (length, ts_code, matched)

        # First scan every alias name in the dict for substring matches
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
        """Exact match (direct dictionary hit)"""
        self.load()
        return self._stock_alias.get(name.strip().lower())

    def count_stocks_in_text(self, text: str) -> int:
        """Number of distinct stocks appearing in the text (dedup by ts_code
        + mutually exclusive spans). Longer aliases claim their span first:
        once "Ping An Bank" matches, its substring "Ping An" (a different
        stock) is no longer counted"""
        self.load()
        text_l = text.lower()
        codes = set()
        occupied = []  # occupied character spans
        for alias, code in sorted(
            self._stock_alias.items(), key=lambda x: -len(x[0])
        ):
            if len(alias) > MAX_NAME_LEN or not alias:
                continue
            idx = text_l.find(alias.lower())
            if idx < 0:
                continue
            span = (idx, idx + len(alias))
            if any(s < span[1] and span[0] < e for s, e in occupied):
                continue  # overlaps a longer alias (substring false hit)
            codes.add(code)
            occupied.append(span)
        return len(codes)

    def get_name(self, ts_code: str) -> Optional[str]:
        self.load()
        return self._code_to_name.get(ts_code)

    # ---------- Metric matching ----------
    # Metric alias -> (canonical field, source table prefix)
    # Reuses the skills.fin_query mapping to avoid duplication
    METRIC_ALIASES: Dict[str, Tuple[str, str]] = {
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

    def match_metric(self, text: str) -> Optional[Tuple[str, str]]:
        """
        Find the longest-matching metric alias in the text
        Returns (canonical field, table prefix) or None
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

    # ---------- Industry matching ----------
    def match_industry(self, text: str) -> Optional[Tuple[str, str]]:
        """
        Find an industry name in the text (Shenwan L1/L2)
        Returns (index_code, industry_name) or None
        Only matches in industry-related context: text containing an industry
        name plus a context word (industry/sector/field)
        """
        self.load()
        # Industry context words: users say "baijiu industry" / "semiconductor sector"
        if not self._industry_dict:
            return None
        # Trigger-word check: an industry/sector/field/track-style word must be
        # present, to avoid misreading an industry into a bare stock mention
        # like "Moutai"
        ctx_words = ["行业", "板块", "领域", "赛道", "概念"]
        if not any(w in text for w in ctx_words):
            return None
        text_l = text.lower()
        best = None
        for name, (code, level) in self._industry_dict.items():
            # Strip the Roman-numeral suffix from industry names: "Baijiu II" variants -> "Baijiu"
            base = _strip_roman_suffix(name)
            if base and base.lower() in text_l:
                if best is None or len(base) > best[0]:
                    best = (len(base), code, name, level)
        if best:
            return best[1], best[2], best[3]  # (index_code, name, level)
        return None

    def all_industries(self) -> List[Tuple[str, str, str]]:
        """All industries (name, code, level)"""
        self.load()
        return [(n, c, lv) for n, (c, lv) in self._industry_dict.items()]


# Singleton
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