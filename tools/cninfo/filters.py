# -*- encoding: utf-8 -*-
from __future__ import annotations

import re
from typing import Dict, List, Optional

EXCLUDE_WORDS = (
    "摘要", "英文", "已取消", "取消", "更正", "更新", "修订", "补充",
    "提示性", "业绩说明会", "董监高", "审计", "督导", "问询", "回复",
    "意见", "制度", "决议", "摘要版", "备案", "鉴证",
)

_EN_REPORT_MARK = re.compile(r"(ANNUAL|QUARTERLY|INTERIM)\s+REPORT", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19[89]\d|20\d{2})")


_CATEGORY_PATTERNS = (
    ("yjdbg", ("第一季度报告", "一季度报告", "一季报")),
    ("sjdbg", ("第三季度报告", "三季度报告", "三季报")),
    ("bndbg", ("半年度报告", "中报")),
    ("ndbg", ("年度报告", "年报")),
)


def classify_category(title: str) -> str:
    """标题 -> 定期报告类别码 (ndbg/bndbg/yjdbg/sjdbg/other)"""
    t = title or ""
    for code, words in _CATEGORY_PATTERNS:
        if any(w in t for w in words):
            return code
    return "other"


def parse_report_year(title: str) -> Optional[int]:
    """标题中的报告年度: '2024年年度报告' -> 2024; 无年份返回 None"""
    m = _YEAR_RE.search(title or "")
    return int(m.group(1)) if m else None


def is_full_periodic_report(title: str) -> bool:
    """是否为定期报告全文 (排除摘要/英文/更正等干扰版本)"""
    t = title or ""
    if classify_category(t) == "other":
        return False
    if any(w in t for w in EXCLUDE_WORDS):
        return False
    if _EN_REPORT_MARK.search(t):
        return False
    return True


def pick_full_report(announcements: List[Dict]) -> Optional[Dict]:
    full = [a for a in announcements
            if is_full_periodic_report(a.get("announcementTitle", ""))]
    if not full:
        return None
    order = {"ndbg": 0, "bndbg": 1, "yjdbg": 2, "sjdbg": 3, "other": 9}
    full.sort(key=lambda a: (
        order.get(classify_category(a.get("announcementTitle", "")), 9),
        -(a.get("announcementTime") or 0)))
    return full[0]


# 6 位代码前缀 -> 交易所后缀 (沪深北市场规则; 沪B 900/深B 200 一并覆盖)
_TS_SUFFIX_RULES = (
    (("60", "68", "90"), "SH"),
    (("00", "30", "20"), "SZ"),
    (("43", "83", "87", "88", "92"), "BJ"),
)


def normalize_ts_code(stock: str) -> Optional[str]:
    s = (stock or "").strip().upper()
    if re.match(r"^\d{6}\.(SH|SZ|BJ)$", s):
        return s
    if not re.match(r"^\d{6}$", s):
        return None
    for prefixes, suffix in _TS_SUFFIX_RULES:
        if s.startswith(prefixes):
            return f"{s}.{suffix}"
    return None
