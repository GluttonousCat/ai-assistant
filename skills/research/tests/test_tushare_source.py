# -*- encoding: utf-8 -*-
"""
T02 offline tests: tushare_source pure-conversion layer + injectable fetching (no DB/LLM)

Covers, by injecting a fake profile dict + FakeRegistry (answering only query_financials):
shard structure / eid uniqueness / source+period non-empty / yuan-div-1e8 conversion
annotation / annual-row preference / mainbiz product-dimension preference / view-section
exclusion / on-disk round-trip.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from skills.research.contracts import load_shard
from skills.research.sources.tushare_source import (
    _EXTRA_BATCHES,
    _period_label,
    collect_tushare_facts,
)

YEARS = ["2021", "2022", "2023", "2024", "2025"]

# fake financial time series (values made up, just to exercise the conversion logic; yuan is the raw unit)
_FIN_BASE = {
    "2021": {"revenue": 109464000000.0, "n_income_attr_p": 52460000000.0,
             "grossprofit_margin": 91.54},
    "2022": {"revenue": 127554000000.0, "n_income_attr_p": 62716000000.0,
             "grossprofit_margin": 91.87},
    "2023": {"revenue": 150560000000.0, "n_income_attr_p": 74734000000.0,
             "grossprofit_margin": 91.96},
    "2024": {"revenue": 174143000000.0, "n_income_attr_p": 86228000000.0,
             "grossprofit_margin": 91.93},
    "2025": {"revenue": 185230000000.0, "n_income_attr_p": 91870000000.0,
             "grossprofit_margin": 92.10},
}


def _base_rows():
    rows = [{"stock_name": "贵州茅台", "end_date": f"{y}-12-31", **_FIN_BASE[y]}
            for y in YEARS]
    # deep-history row (asserts the series is clipped to the most recent `years` annual years)
    rows.append({"stock_name": "贵州茅台", "end_date": "2014-12-31",
                 "revenue": 32217000000.0, "n_income_attr_p": 15350000000.0,
                 "grossprofit_margin": 91.70})
    # same-year multi-period: a 2024 interim row (asserts annual-row preference);
    # 2026 has only an interim row (asserts the H1 label)
    rows.append({"stock_name": "贵州茅台", "end_date": "2024-06-30",
                 "revenue": 81900000000.0, "n_income_attr_p": 41700000000.0,
                 "grossprofit_margin": 91.60})
    rows.append({"stock_name": "贵州茅台", "end_date": "2026-06-30",
                 "revenue": 98000000000.0, "n_income_attr_p": 48100000000.0,
                 "grossprofit_margin": 92.00})
    return rows


def _extra_rows():
    """One row per year per fill-in batch; each metric's bare column gets made-up values
    (amount-type columns in yuan)"""
    vals = {
        "roe": [27.68, 29.90, 32.09, 34.71, 36.42],
        "netprofit_margin": [47.92, 49.15, 49.64, 49.51, 49.60],
        "debt_to_assets": [23.20, 22.85, 17.94, 14.92, 14.08],
        "n_cashflow_act": [64029000000.0, 66690000000.0, 66593000000.0,
                           92460000000.0, 96900000000.0],
        "total_assets": [255160000000.0, 275060000000.0, 301980000000.0,
                         338640000000.0, 361000000000.0],
        "total_hldr_eqy_exc_min_int": [190510000000.0, 209030000000.0, 237940000000.0,
                                       273420000000.0, 291070000000.0],
        "basic_eps": [41.76, 49.93, 59.49, 68.64, 73.11],
        "or_yoy": [11.71, 16.51, 18.03, 15.66, 6.36],
        "netprofit_yoy": [12.33, 19.54, 19.16, 15.38, 6.55],
        "net_after_nr_lp_correct": [52400000000.0, 62700000000.0,
                                    74300000000.0, 86000000000.0,
                                    91500000000.0],
        "operate_profit": [68500000000.0, 81700000000.0, 97200000000.0,
                           112000000000.0, 119000000000.0],
        "inventories": [33300000000.0, 35600000000.0, 39500000000.0,
                        43200000000.0, 47000000000.0],
    }
    # organized as {metrics tuple: [rows...]} — each row carries all bare columns of its batch
    table = {}
    for batch in _EXTRA_BATCHES:
        bares = [m["bare"] for m in batch]
        rows = []
        for i, y in enumerate(YEARS):
            row = {"stock_name": "贵州茅台", "end_date": f"{y}-12-31"}
            for b in bares:
                row[b] = vals[b][i]
            rows.append(row)
        table[tuple(m["query"] for m in batch)] = rows
    # quote snapshot (total market cap): latest 2 trading days, newest first
    table[("总市值",)] = [
        {"trade_date": "2026-09-24", "stock_name": "贵州茅台", "close": 1470.5,
         "pe_ttm": 18.99, "pb": 7.5, "total_mv": 18445100.0,
         "circ_mv": 18445100.0, "turnover_rate": 0.31},
        {"trade_date": "2026-09-23", "stock_name": "贵州茅台", "close": 1465.0,
         "pe_ttm": 18.92, "pb": 7.47, "total_mv": 18376050.0,
         "circ_mv": 18376050.0, "turnover_rate": 0.28},
    ]
    return table


class FakeRegistry:
    """Answers only query_financials (looked up by the metrics tuple), records calls for assertions"""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def call(self, tool, args):
        self.calls.append((tool, dict(args)))
        if tool != "query_financials":
            return {"ok": False, "error": f"测试不支持工具 {tool}"}
        rows = self.table.get(tuple(args.get("metrics") or []), [])
        return {"ok": True, "data": {"columns": [], "rows": len(rows),
                                     "data": rows, "truncated": False}}


@pytest.fixture()
def fake_profile():
    return {
        "stock": "贵州茅台", "ts_code": "600519.SH", "name": "贵州茅台",
        "years": 5, "generated_at": "2026-09-25T08:00:00",
        "ok_sections": ["basic", "main_business", "financials", "valuation",
                        "forecasts", "verification", "risk", "reports",
                        "regime", "industry", "holders"],
        "failed_sections": [],
        "sections": {
            "basic": {"stocks": [{
                "ts_code": "600519.SH", "name": "贵州茅台",
                "matched": "贵州茅台", "industry": "白酒",
                "industry_l1": "食品饮料", "industry_l2": "白酒",
                "market": "主板"}]},
            "main_business": {
                "end_date": "2025-12-31",
                "data": [
                    {"bz_code": "P", "bz_item": "茅台酒", "bz_sales": 145800000000.0,
                     "sales_share_pct": 83.9, "gross_margin_pct": 94.3},
                    {"bz_code": "P", "bz_item": "系列酒", "bz_sales": 24500000000.0,
                     "sales_share_pct": 14.1, "gross_margin_pct": 79.8},
                    {"bz_code": "P", "bz_item": "其他业务", "bz_sales": 3400000000.0,
                     "sales_share_pct": 2.0, "gross_margin_pct": None},
                    {"bz_code": "D", "bz_item": "国内", "bz_sales": 170000000000.0,
                     "sales_share_pct": 97.9, "gross_margin_pct": None},
                ]},
            "financials": {"data": _base_rows()},
            "valuation": {
                "years": 3,
                "pe_ttm": {"latest": 20.15, "pct_rank": 35.2, "min": 15.3,
                           "max": 45.6, "n_days": 718},
                "pb": {"latest": 7.82, "pct_rank": 28.4, "min": 5.9,
                       "max": 16.2, "n_days": 718}},
            "holders": {"end_date": "2026-06-30", "holders": [
                {"holder_name": "中国贵州茅台酒厂(集团)有限责任公司",
                 "hold_ratio": 54.07, "hold_change": None},
                {"holder_name": "香港中央结算有限公司",
                 "hold_ratio": 6.12, "hold_change": -1200.0},
                {"holder_name": "贵州省国有资本运营有限责任公司",
                 "hold_ratio": 4.54, "hold_change": None},
                {"holder_name": "中国证券金融股份有限公司",
                 "hold_ratio": 2.98, "hold_change": None},
                {"holder_name": "茅台酒技术开发公司",
                 "hold_ratio": 2.05, "hold_change": None}]},
            "industry": {"data": [
                {"stock_name": "贵州茅台", "value": 36.42, "end_date": "2025-12-31"},
                {"stock_name": "泸州老窖", "value": 30.11},
                {"stock_name": "五粮液", "value": 24.30},
                {"stock_name": "山西汾酒", "value": 22.15}]},
            # view/judgment-layer sections — must be excluded
            "forecasts": {"divergence_stats": [{"org": "XX证券", "eps": 75.0}]},
            "verification": {"verified_groups": [], "hit_rate_10pct": 60},
            "risk": {"alerts": [{"rule": "r1", "level": "warn", "message": "m"}]},
            "reports": {"reports": [{"org": "YY证券", "title": "t", "rating": "买入"}]},
            "regime": {"state": "trend"},
        },
    }


@pytest.fixture()
def shard(tmp_path, fake_profile):
    reg = FakeRegistry(_extra_rows())
    return collect_tushare_facts(
        "贵州茅台", years=5, out_dir=tmp_path,
        profile=fake_profile, registry=reg)


# ---------- shard structure ----------

def test_shard_kind_and_volume(shard):
    assert shard.kind == "tushare"
    assert shard.ts_code == "600519.SH"
    assert len(shard.facts) >= 60
    assert shard.views == []          # pure fact layer, no views
    assert shard.meta["fact_count"] == len(shard.facts)
    assert shard.meta["view_count"] == 0


def test_every_fact_source_period_nonempty(shard):
    for f in shard.facts:
        assert f.source.strip(), f"source 为空: {f.eid}"
        assert f.period.strip(), f"period 为空: {f.eid}"
        assert f.content.strip(), f"content 为空: {f.eid}"


def test_eid_unique(shard):
    eids = [f.eid for f in shard.facts]
    assert len(eids) == len(set(eids)), "eid 重复"


def test_all_fact_layer_with_ts_code(shard):
    for f in shard.facts:
        assert f.layer == "fact"
        assert f.ts_code == "600519.SH"


def test_meta_covers_caliber(shard):
    assert "caliber" in shard.meta and "÷1e8" in shard.meta["caliber"]
    assert "excluded" in shard.meta
    assert shard.meta["snapshot_date"] == "2026-09-25"
    assert len(shard.meta["coverage"]["tushare_metrics"]) >= 12
    # fake data has values for every metric -> no missing records
    assert shard.meta["coverage"]["metrics_missing_data"] == []


def test_missing_metric_recorded_not_fabricated(tmp_path, fake_profile):
    """A metric with no values across all periods -> skipped and recorded in meta, never fabricated"""
    table = _extra_rows()
    for rows in table.values():                    # wipe the roe column
        for r in rows:
            r["roe"] = None
    reg = FakeRegistry(table)
    s = collect_tushare_facts("贵州茅台", years=5, out_dir=tmp_path,
                              profile=fake_profile, registry=reg)
    assert not any(f.eid.startswith("T:tushare.roe.") for f in s.facts)
    assert "roe" in s.meta["coverage"]["metrics_missing_data"]


# ---------- conversion and caliber ----------

def test_yuan_conversion_marked(shard):
    by_eid = {f.eid: f for f in shard.facts}
    rev = by_eid["T:tushare.revenue.2024A"]
    assert rev.value == pytest.approx(1741.43, abs=0.01)
    assert rev.unit == "亿元"
    assert rev.confidence == 0.9
    assert "÷1e8" in rev.source
    assert "fin.income.revenue" in rev.source
    assert "1741.43" in rev.content

    roe = by_eid["T:tushare.roe.2024A"]
    assert roe.value == pytest.approx(34.71)
    assert roe.confidence == 1.0
    assert roe.unit == "%"


def test_annual_row_preferred_and_h1_label(shard):
    by_eid = {f.eid: f for f in shard.facts}
    # 2024 has an interim row the same year; the annual row must win (1741.43 not 819)
    assert by_eid["T:tushare.revenue.2024A"].value == pytest.approx(1741.43, abs=0.01)
    # 2026 has only an interim row -> H1 label (cumulative caliber, not annualized)
    assert "T:tushare.revenue.2026H1" in by_eid
    assert "中报" in by_eid["T:tushare.revenue.2026H1"].content


def test_timeseries_clipped_to_years(shard):
    """Deep history (2014) must be clipped; keep the latest 5 annual years + the current
    in-progress interim period"""
    eids = {f.eid for f in shard.facts}
    assert not any("2014A" in e for e in eids)
    for y in YEARS:
        assert f"T:tushare.revenue.{y}A" in eids
    assert "T:tushare.revenue.2026H1" in eids


def test_period_label_pure():
    assert _period_label("2024-12-31") == "2024A"
    assert _period_label("2024-06-30") == "2024H1"
    assert _period_label("2024-03-31") == "2024Q1"
    assert _period_label("2024-09-30") == "2024Q3"
    assert _period_label("2024-05-15") == "2024-05-15"


def test_mainbiz_prefers_product_dim(shard):
    mb = [f for f in shard.facts if f.eid.startswith("T:profile.mainbiz.")]
    assert len(mb) == 3                      # only the 3 product-dimension rows; region dimension dropped
    assert not any("国内" in f.content for f in mb)
    first = {f.eid: f for f in mb}["T:profile.mainbiz.2025A.1"]
    assert "茅台酒" in first.content
    assert first.value == pytest.approx(1458.0, abs=0.01)
    assert "83.9" in first.content


# ---------- view-layer exclusion ----------

def test_opinion_sections_excluded(shard):
    joined = " ".join(f.content for f in shard.facts)
    for kw in ("预测", "评级", "买入", "研报"):
        assert kw not in joined, f"事实层混入观点关键词「{kw}」"


# ---------- on-disk round trip ----------

def test_save_and_load_roundtrip(shard, tmp_path):
    path = tmp_path / "facts_tushare.json"
    assert path.exists()
    loaded = load_shard(path)
    assert loaded.kind == "tushare"
    assert len(loaded.facts) == len(shard.facts)
    assert loaded.facts[0].eid == shard.facts[0].eid
    assert shard.meta["path"] == str(path)


def test_market_snapshot_facts(shard):
    """Market-cap snapshot: 10k-yuan /1e4 into 100M yuan confidence=0.9; close quoted
    verbatim 1.0; takes the latest trading day"""
    by = {f.eid: f for f in shard.facts}
    mv = by["T:tushare.val_mv.2026-09-24"]
    assert mv.value == pytest.approx(1844.51, abs=0.01)
    assert mv.unit == "亿元" and mv.confidence == 0.9
    assert "÷1e4" in mv.source and "stock.daily_basic.total_mv" in mv.source
    assert mv.period == "2026-09-24"
    close = by["T:tushare.val_close.2026-09-24"]
    assert close.value == pytest.approx(1470.5)
    assert close.confidence == 1.0 and close.unit == "元"
    assert "T:tushare.val_mv.2026-09-23" not in by   # non-latest trading day not taken


def test_fetch_batches_called(tmp_path, fake_profile):
    reg = FakeRegistry(_extra_rows())
    collect_tushare_facts("贵州茅台", years=5, out_dir=tmp_path,
                          profile=fake_profile, registry=reg)
    fin_calls = [c for c in reg.calls if c[0] == "query_financials"]
    batch_calls = [c for c in fin_calls
                   if c[1].get("metrics") != ["总市值"]]
    assert len(batch_calls) == len(_EXTRA_BATCHES)
    assert all("最近5年" in c[1]["time"] for c in batch_calls)
    # the quote snapshot is a separate single call (no time argument)
    assert any(c[1].get("metrics") == ["总市值"] for c in fin_calls)
