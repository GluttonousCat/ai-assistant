# -*- encoding: utf-8 -*-
"""巨潮公告筛选纯函数测试 (无网络/无 DB)"""
from __future__ import annotations

from tools.cninfo.filters import (classify_category, is_full_periodic_report,
                                  parse_report_year, pick_full_report)


def test_classify_category():
    assert classify_category("2024年年度报告") == "ndbg"
    assert classify_category("2024年半年度报告") == "bndbg"
    assert classify_category("2024年第一季度报告") == "yjdbg"
    assert classify_category("2024年第三季度报告") == "sjdbg"
    assert classify_category("关于召开股东大会的通知") == "other"
    assert classify_category("") == "other"


def test_parse_report_year():
    assert parse_report_year("2024年年度报告") == 2024
    assert parse_report_year("中际旭创：2023年半年度报告（更新后）") == 2023
    assert parse_report_year("年度报告") is None
    assert parse_report_year("") is None


def test_is_full_periodic_report_excludes_noise():
    # 正式全文
    assert is_full_periodic_report("2024年年度报告")
    assert is_full_periodic_report("2024年半年度报告")
    # 干扰版本
    assert not is_full_periodic_report("2024年年度报告摘要")
    assert not is_full_periodic_report("2024年年度报告（更新后）")
    assert not is_full_periodic_report("2024年年度报告（已取消）")
    assert not is_full_periodic_report("2024 ANNUAL REPORT")          # 英文版
    assert not is_full_periodic_report("关于2024年年度审计报告的说明")
    assert not is_full_periodic_report("2024年年度股东大会决议公告")  # other 类


def _ann(title, t=1700000000000, aid="x"):
    return {"announcementTitle": title, "announcementTime": t,
            "announcementId": aid, "adjunctUrl": f"finalpage/{aid}.PDF"}


def test_pick_full_report_priority_and_recency():
    anns = [
        _ann("2024年年度报告摘要", aid="a1"),
        _ann("2024年年度报告", t=1700000001000, aid="a2"),
        _ann("2023年年度报告", t=1600000000000, aid="a3"),
    ]
    # 年报类别优先于... 全是年报时取时间最新
    best = pick_full_report(anns)
    assert best["announcementId"] == "a2"
    # 年报优先于季报
    anns2 = [_ann("2024年第三季度报告", t=1800000000000, aid="q3"),
             _ann("2023年年度报告", t=1600000000000, aid="y")]
    assert pick_full_report(anns2)["announcementId"] == "y"
    # 全是噪音 -> None
    assert pick_full_report([_ann("2024年年度报告摘要")]) is None
    assert pick_full_report([]) is None
