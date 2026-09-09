# -*- encoding: utf-8 -*-
"""
巨潮公告筛选纯函数 (无 IO, 可测)

职责: 从查询结果中识别"正式定期报告全文", 排除摘要/英文版/更正版等干扰项。
取舍说明: 更正/更新版本默认排除 (保留首发版本入库, 交叉核验以首发口径为准;
若需追最终版, 元数据已留痕可按 title 人工定位)。
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# 标题含任一即排除 (非报告全文/干扰项)
EXCLUDE_WORDS = (
    "摘要", "英文", "已取消", "取消", "更正", "更新", "修订", "补充",
    "提示性", "业绩说明会", "董监高", "审计", "督导", "问询", "回复",
    "意见", "制度", "决议", "摘要版", "备案", "鉴证",
)
# 英文年报标题特征 (如 '2024 ANNUAL REPORT')
_EN_REPORT_MARK = re.compile(r"(ANNUAL|QUARTERLY|INTERIM)\s+REPORT", re.IGNORECASE)
_YEAR_RE = re.compile(r"(19[89]\d|20\d{2})")

# 标题词 -> 巨潮类别 (与查询 category 参数对应的后缀语义一致)
# 顺序敏感: 半年度/季报在前, 年报最后 — "半年度报告" 含子串 "年度报告",
# 先匹配短类别避免误归年报
_CATEGORY_PATTERNS = (
    ("yjdbg", ("第一季度报告", "一季报")),
    ("sjdbg", ("第三季度报告", "三季报")),
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
    """从单次查询结果中选出报告全文 (类别最高优先: 年报 > 半年报 > 季报;
    同类多条取 announcementTime 最新)"""
    full = [a for a in announcements
            if is_full_periodic_report(a.get("announcementTitle", ""))]
    if not full:
        return None
    order = {"ndbg": 0, "bndbg": 1, "yjdbg": 2, "sjdbg": 3, "other": 9}
    full.sort(key=lambda a: (
        order.get(classify_category(a.get("announcementTitle", "")), 9),
        -(a.get("announcementTime") or 0)))
    return full[0]
