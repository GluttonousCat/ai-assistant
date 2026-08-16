"""
规则查询引擎 (Rule Query Engine)
基于 StockKB 词典匹配 + SQL 模板拼接, 覆盖封闭域固定句式

三态返回:
- ("sql", sql, meta)
- ("unsupported", None)   -> 规则不支持, 交棒 LLM
- ("error", None)         -> 词典可用但参数异常 (如股票不存在)

能力:
- 单股单指标 [时间] 查询 (时间含 年报/季报/半年报/去年/最近N年)
- 单股多指标 (最多 3 列, 不再丢弃后续指标)
- 多股同指标对比 (识别 "和/与/、/谁更高")
"""
from __future__ import annotations

import re
from datetime import date
from typing import Dict, List, Optional, Tuple

from tools.finance.stock_kb import get_stock_kb, parse_time_phrase
from tools.finance.sql_builder import build_single_query, build_compare_query

# 动作词 (统一清理)
_ACTION_WORDS = [
    "帮我查一下", "帮我查询", "帮我查", "请问", "查一下", "查一查",
    "查询", "看看", "查看", "请查", "帮我", "请", "查", "看",
    "的", "、", "和", "与", "哪个", "谁", "更高", "更高", "对比", "比较",
    "最近", "最新", "近",
]
# 时间词 (统一清理)
_TIME_WORDS = [
    "最近一年", "最近两年", "最近三年", "近一年", "近两年", "近三年",
    "今年以来", "去年", "今年", "上季度", "本季度",
]


def _clean_entity_text(text: str) -> str:
    """清理文本中的动作词/连接词/时间词"""
    t = text
    for w in _ACTION_WORDS:
        t = t.replace(w, "")
    for w in _TIME_WORDS:
        t = t.replace(w, "")
    t = re.sub(r"(19|20)\d{2}\s*年", "", t)
    return t.strip()


class RuleEngine:
    """规则查询引擎"""

    def __init__(self):
        self.kb = get_stock_kb()
        self.kb.load()  # 确保词典已加载

    def parse(self, user_input: str):
        """
        解析查询 -> SQL
        返回 (sql, meta_dict) 或 (None, {"reason": "unsupported"|"error"})
        """
        text = user_input.strip()
        if not text:
            return None, {"reason": "unsupported"}

        # 1. 股票实体 (可能多个: 对比)
        stocks = self._extract_stocks(text)

        # 2. 指标实体 (可能多个: 多指标)
        metrics = self._extract_metrics(text)

        if not stocks:
            return None, {"reason": "unsupported"}
        if not metrics:
            return None, {"reason": "unsupported"}

        time_phrase = parse_time_phrase(text)
        # 相对时间 -> 绝对年份 ("去年" = 上一年年报)
        if time_phrase and time_phrase[0] == "previous_year":
            time_phrase = ("year", date.today().year - 1)
        elif time_phrase and time_phrase[0] == "this_year":
            # 今年尚未出年报, 退化为最新期
            time_phrase = ("recent", None)

        # 3. 意图: 对比 (>=2 股票) or 单股
        if len(stocks) >= 2:
            # 取第一个指标做对比
            field, prefix = metrics[0]
            sql = build_compare_query(stocks, field, prefix, time_phrase)
            if sql:
                return sql, {"type": "compare", "stocks": [s[1] for s in stocks],
                             "metric": field}
            return None, {"reason": "error"}

        # 4. 单股: 单指标或多指标 (多列 SELECT, 不丢弃)
        ts_code, name = stocks[0]
        field, prefix = metrics[0]
        extra = [m for m in metrics[1:3]]
        sql = build_single_query(ts_code, name, field, prefix, time_phrase,
                                 extra_fields=extra)
        if sql:
            meta = {"type": "query", "stock": name, "metric": field}
            if extra:
                meta["extra_metrics"] = [e[0] for e in extra]
            return sql, meta
        return None, {"reason": "error"}

    # ---------- 实体提取 ----------
    def _extract_stocks(self, text: str) -> List[Tuple[str, str]]:
        """提取文本中的所有股票 (ts_code, 标准名)"""
        found: List[Tuple[str, str]] = []
        # 方法: 对每个词典别名, 若在文本中且未被覆盖, 记录位置
        # 简化: 匹配所有出现在文本中的别名, 去重
        codes_seen = set()
        # 按别名长度降序, 优先长名 (全称 > 简称)
        for alias, code in sorted(
            self.kb._stock_alias.items(), key=lambda x: -len(x[0])
        ):
            if code in codes_seen:
                continue  # 同一股票只取一个别名
            if alias and alias.lower() in text.lower():
                # 记录该股票的标准名
                name = self.kb.get_name(code) or alias
                found.append((code, name))
                codes_seen.add(code)
                # 匹配后从文本移除, 避免影响其他别名
                text_clean = text
                text = text.replace(alias, " ")
                _ = text_clean
                if len(found) >= 3:
                    break
        return found

    def _extract_metrics(self, text: str) -> List[Tuple[str, str]]:
        """提取文本中的指标 (field, prefix), 最多 3 个.
        按别名长度降序匹配, 已命中区间不再重复匹配
        (避免 "赚了多少钱" 命中净利润后, 子串 "多少钱" 再命中股价)"""
        found = []
        seen = set()
        occupied = []  # 已占用字符区间 [(start, end), ...]
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
                continue  # 与已命中区间重叠
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