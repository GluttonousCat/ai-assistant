"""
Rule query engine
StockKB dictionary matching + SQL template stitching; covers fixed closed-domain phrasings

Three-state return:
- ("sql", sql, meta)
- ("unsupported", None)   -> rule does not apply, hand off to the LLM
- ("error", None)         -> dictionary usable but bad parameters (e.g. stock not found)

Capabilities:
- Single stock, single metric [time] query (time covers annual/quarterly/semi-annual
  reports / last year / last N years)
- Single stock, multiple metrics (up to 3 columns; later metrics are no longer dropped)
- Multiple stocks, same metric comparison (recognizes "和/与/、/谁更高")
"""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from tools.finance.stock_kb import get_stock_kb, parse_time_phrase
from tools.finance.sql_builder import build_single_query, build_compare_query

# Action words (stripped uniformly)
_ACTION_WORDS = [
    "帮我查一下", "帮我查询", "帮我查", "请问", "查一下", "查一查",
    "查询", "看看", "查看", "请查", "帮我", "请", "查", "看",
    "的", "、", "和", "与", "哪个", "谁", "更高", "更高", "对比", "比较",
    "最近", "最新", "近",
]
# Time words (stripped uniformly)
_TIME_WORDS = [
    "最近一年", "最近两年", "最近三年", "近一年", "近两年", "近三年",
    "今年以来", "去年", "今年", "上季度", "本季度",
]


def _clean_entity_text(text: str) -> str:
    """Strip action/connective/time words from the text"""
    t = text
    for w in _ACTION_WORDS:
        t = t.replace(w, "")
    for w in _TIME_WORDS:
        t = t.replace(w, "")
    t = re.sub(r"(19|20)\d{2}\s*年", "", t)
    return t.strip()


# Ranking/filtering patterns (whole market or industry)
_TOP_N_PAT = re.compile(
    r"最高的?\s*(\d{1,3})\s*[家只个股]?\s*$|排名前\s*(\d{1,3})|前\s*(\d{1,3})\s*名|top\s*(\d{1,3})",
    re.IGNORECASE,
)
_COND_PAT = re.compile(
    r"(大于等于|小于等于|不低于|不超过|不高于|大于|小于|超过|高于|低于)\s*([\d.]+)\s*%?"
)
_COND_OP_MAP = {
    "大于": "gt", "高于": "gt", "超过": "gt",
    "小于": "lt", "低于": "lt",
    "大于等于": "ge", "不低于": "ge",
    "小于等于": "le", "不超过": "le", "不高于": "le",
}


def _parse_rank_params(text: str) -> Optional[Dict]:
    """Parse ranking/filtering parameters. Returns {top_n, op, value} or None (not a ranking/filtering phrase)"""
    top_n, op, value = None, None, None
    m = _COND_PAT.search(text)
    if m:
        op = _COND_OP_MAP.get(m.group(1), "gt")
        value = float(m.group(2))
    m2 = _TOP_N_PAT.search(text)
    if m2:
        top_n = int(next(g for g in m2.groups() if g))
    if top_n is None and op is None:
        return None
    return {"top_n": top_n or 10, "op": op, "value": value}


class RuleEngine:
    """Rule query engine"""

    def __init__(self):
        self.kb = get_stock_kb()
        self.kb.load()  # ensure the dictionary is loaded

    def parse(self, user_input: str, context_stocks: Optional[List[Tuple[str, str]]] = None):
        """
        Parse a query -> SQL
        context_stocks: stocks from the previous conversation turn [(ts_code, name)];
        inherited when the current phrase omits the subject
        Returns (sql, meta_dict) or (None, {"reason": "unsupported"|"error"})
        """
        text = user_input.strip()
        if not text:
            return None, {"reason": "unsupported"}

        # 1. Stock entities (possibly multiple: comparison)
        stocks = self._extract_stocks(text)

        # 2. Metric entities (possibly multiple: multi-metric)
        metrics = self._extract_metrics(text)

        # 3. Industry entity ("YY metric of XX industry")
        industry = self.kb.match_industry(text)

        time_phrase = parse_time_phrase(text)
        # Relative time -> absolute year ("去年" = previous year's annual report)
        if time_phrase and time_phrase[0] == "previous_year":
            time_phrase = ("year", date.today().year - 1)
        elif time_phrase and time_phrase[0] == "this_year":
            # No annual report for this year yet, degrade to the latest period
            time_phrase = ("recent", None)

        rank_params = _parse_rank_params(text)

        # No stock but an industry: industry query / industry ranking / industry filtering
        if not stocks and industry and metrics:
            from tools.finance.sql_builder import build_industry_query
            index_code, ind_name, level = industry
            field, prefix = metrics[0]
            if rank_params:
                sql = build_industry_query(
                    index_code, field, prefix, time_phrase, level=level,
                    top_n=rank_params["top_n"],
                    op=rank_params["op"], value=rank_params["value"],
                )
            else:
                sql = build_industry_query(index_code, field, prefix, time_phrase,
                                           level=level)
            if sql:
                return sql, {"type": "industry", "industry": ind_name,
                             "metric": field}
            return None, {"reason": "error"}

        # No stock but a metric + ranking/filtering pattern: whole-market ranking
        if not stocks and metrics and rank_params:
            from tools.finance.sql_builder import build_rank_query
            field, prefix = metrics[0]
            sql = build_rank_query(
                field, prefix,
                top_n=rank_params["top_n"],
                op=rank_params["op"], value=rank_params["value"],
                time_phrase=time_phrase,
            )
            if sql:
                meta = {"type": "rank", "metric": field,
                        "top_n": rank_params["top_n"]}
                if rank_params["op"]:
                    meta["filter"] = {"op": rank_params["op"],
                                      "value": rank_params["value"]}
                return sql, meta
            return None, {"reason": "error"}

        # No stock + metric present + session context stocks: inherit the previous
        # subject (follow-up question scenario)
        context_inherited = False
        if not stocks and metrics and context_stocks:
            stocks = list(context_stocks)[:2]
            context_inherited = True

        if not stocks:
            return None, {"reason": "unsupported", "missing": "stock"}
        if not metrics:
            return None, {"reason": "unsupported", "missing": "metric"}

        # 4. Intent: comparison (>=2 stocks) or single stock
        if len(stocks) >= 2:
            # Compare on the first metric
            field, prefix = metrics[0]
            sql = build_compare_query(stocks, field, prefix, time_phrase)
            if sql:
                return sql, {"type": "compare", "stocks": [s[1] for s in stocks],
                             "metric": field}
            return None, {"reason": "error"}

        # 5. Single stock: single or multiple metrics (multi-column SELECT, nothing dropped)
        ts_code, name = stocks[0]
        field, prefix = metrics[0]
        extra = [m for m in metrics[1:3]]
        sql = build_single_query(ts_code, name, field, prefix, time_phrase,
                                 extra_fields=extra)
        if sql:
            meta = {"type": "query", "stock": name, "metric": field}
            if extra:
                meta["extra_metrics"] = [e[0] for e in extra]
            if context_inherited:
                meta["context_inherited"] = True
            return sql, meta
        return None, {"reason": "error"}

    # ---------- Entity extraction ----------
    def _extract_stocks(self, text: str) -> List[Tuple[str, str]]:
        """Extract all stocks in the text (ts_code, canonical name)"""
        found: List[Tuple[str, str]] = []
        # Method: for each dictionary alias, record its position if present in the
        # text and not yet covered
        # Simplified: match every alias appearing in the text, dedupe
        codes_seen = set()
        # Sort aliases by length descending, prefer long names (full name > short name)
        for alias, code in sorted(
            self.kb._stock_alias.items(), key=lambda x: -len(x[0])
        ):
            if code in codes_seen:
                continue  # take only one alias per stock
            if alias and alias.lower() in text.lower():
                # record the stock's canonical name
                name = self.kb.get_name(code) or alias
                found.append((code, name))
                codes_seen.add(code)
                # remove the matched alias from the text so it cannot match other aliases
                text = text.replace(alias, " ")
                if len(found) >= 3:
                    break
        return found

    def _extract_metrics(self, text: str) -> List[Tuple[str, str]]:
        """Extract metrics in the text (field, prefix), at most 3.
        Match aliases by length descending; already-matched spans are not
        re-matched (avoids "赚了多少钱" hitting net profit, then the substring
        "多少钱" hitting stock price)"""
        found = []
        seen = set()
        occupied = []  # occupied character spans [(start, end), ...]
        text_l = text.lower()
        for alias, (field, prefix) in sorted(
            self.kb.METRIC_ALIASES.items(), key=lambda x: -len(x[0])
        ):
            if (field, prefix) in seen:
                continue
            idx = text_l.find(alias.lower())
            if idx < 0:
                continue
            span = (idx, idx + len(alias))
            if any(s < span[1] and span[0] < e for s, e in occupied):
                continue  # overlaps an already-matched span
            found.append((field, prefix))
            seen.add((field, prefix))
            occupied.append(span)
            if len(found) >= 3:
                break
        return found


_engine: Optional[RuleEngine] = None


def get_rule_engine() -> RuleEngine:
    global _engine
    if _engine is None:
        _engine = RuleEngine()
    return _engine


if __name__ == "__main__":
    eng = get_rule_engine()
    for q in [
        "生益科技2023年毛利率",
        "兆易的营收",
        "沪电股份的净利润",
        "贵州茅台市值",
        "对比招商银行和工商银行的市盈率",
    ]:
        sql, meta = eng.parse(q)
        print(f"=== {q} ===")
        print(f"  meta={meta}")
        print(f"  sql={'...' if sql else None}")
        if sql:
            print(f"  {sql[:120]}")
        print()