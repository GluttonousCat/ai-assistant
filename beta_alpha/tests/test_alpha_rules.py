"""alpha 四象限规则测试 (纯函数, 无 DB)"""
from __future__ import annotations

from beta_alpha.skills.alpha import classify_drift, edge_trend, to_yuan


def test_to_yuan_unit_factors():
    assert to_yuan(1.5, "亿元") == 1.5e8
    assert to_yuan(2, "万元") == 2e4
    assert to_yuan(3, "百万元") == 3e6
    assert to_yuan(0.4, "万亿元") == 4e11
    # 增速/倍数不换算
    assert to_yuan(25.0, "%") == 25.0
    assert to_yuan(1.2, "倍") == 1.2
    # 未知/空单位保守原值
    assert to_yuan(7, None) == 7.0
    assert to_yuan(7, "火星币") == 7.0


def test_to_yuan_strips_whitespace():
    assert to_yuan(1.0, " 亿元 ") == 1e8


def test_classify_drift_direction():
    assert classify_drift(100, 110) == "上修"     # +10%
    assert classify_drift(100, 88) == "下修"      # -12%
    assert classify_drift(100, 101.5) == "持平"   # +1.5% < 3
    assert classify_drift(100, 98.5) == "持平"
    # 负基数 (亏损收窄/扩大) 以绝对值为分母
    assert classify_drift(-100, -80) == "上修"    # 亏损收窄 20%
    assert classify_drift(-100, -120) == "下修"   # 亏损扩大


def test_classify_drift_insufficient_data():
    assert classify_drift(None, 100) is None
    assert classify_drift(100, None) is None
    assert classify_drift(0, 100) is None


def test_edge_trend_high_level_context():
    # 高增速下的边际回落 -> "高位降速" 而非裸"降速" (避免误读)
    assert edge_trend(182.49, 192.12) == "高位降速"
    assert edge_trend(262.28, 241.70) == "高位提速"
    assert edge_trend(60.0, 60.0) == "高位平稳"


def test_edge_trend_directions():
    assert edge_trend(20.0, 10.0) == "提速"
    assert edge_trend(10.0, 20.0) == "降速"
    assert edge_trend(12.0, 10.0) == "平稳"   # +2pp < 5pp


def test_edge_trend_negative_growth():
    assert edge_trend(-5.0, -20.0) == "负增收窄"
    assert edge_trend(-30.0, -10.0) == "负增扩大"
    assert edge_trend(-11.0, -10.0) == "负增持平"


def test_edge_trend_missing():
    assert edge_trend(None, 10.0) is None
    assert edge_trend(10.0, None) is None
