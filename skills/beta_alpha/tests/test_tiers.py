"""Mapping tier rule tests (pure function, no DB)"""
from __future__ import annotations

from skills.beta_alpha.analysis.chain_analysis import (
    MEDIUM_SHARE,
    STRONG_SHARE,
    assign_tier,
)


def test_strong_by_mainbz_share():
    assert assign_tier(share=STRONG_SHARE, sw_hit=False, hits=0) == "强"
    assert assign_tier(share=96.1, sw_hit=False, hits=0) == "强"
    assert assign_tier(share=100.0, sw_hit=True, hits=9) == "强"


def test_medium_by_share_or_industry_plus_reports():
    assert assign_tier(share=MEDIUM_SHARE, sw_hit=False, hits=0) == "中"
    assert assign_tier(share=29.9, sw_hit=False, hits=0) == "中"
    # no main-business evidence: industry hit plus >=2 reports is the only way to medium
    assert assign_tier(share=None, sw_hit=True, hits=2) == "中"
    assert assign_tier(share=None, sw_hit=True, hits=1) == "弱"


def test_weak_when_no_hard_evidence():
    assert assign_tier(share=None, sw_hit=False, hits=5) == "弱"
    assert assign_tier(share=None, sw_hit=False, hits=0) == "弱"
    # main-business share below the medium threshold
    assert assign_tier(share=9.9, sw_hit=False, hits=0) == "弱"


def test_thresholds_unchanged():
    assert STRONG_SHARE == 30
    assert MEDIUM_SHARE == 10
