# -*- encoding: utf-8 -*-
"""
T02 离线测试: tushare_source 纯转换层 + 可注入取数 (不连 DB/LLM)

通过注入假画像 dict + FakeRegistry (仅响应 query_financials) 覆盖:
分片结构 / eid 唯一 / source+period 非空 / 金额÷1e8 换算标注 /
年报行优先 / 主营产品维度优先 / 观点板块排除 / 落盘 round-trip。
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

# 假财务时序 (数值编造, 只为验证转换逻辑; 元为原值单位)
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
    # 深历史行 (断言时序截断到最近 years 个年报年度)
    rows.append({"stock_name": "贵州茅台", "end_date": "2014-12-31",
                 "revenue": 32217000000.0, "n_income_attr_p": 15350000000.0,
                 "grossprofit_margin": 91.70})
    # 同年多期: 2024 年插入一行中报 (断言年报行优先); 2026 年只有中报 (断言 H1 标签)
    rows.append({"stock_name": "贵州茅台", "end_date": "2024-06-30",
                 "revenue": 81900000000.0, "n_income_attr_p": 41700000000.0,
                 "grossprofit_margin": 91.60})
    rows.append({"stock_name": "贵州茅台", "end_date": "2026-06-30",
                 "revenue": 98000000000.0, "n_income_attr_p": 48100000000.0,
                 "grossprofit_margin": 92.00})
    return rows


def _extra_rows():
    """每补查批一行/年; 各指标 bare 列名编造数值 (金额类单位元)"""
    vals = {
        "roe": [27.68, 29.90, 32.09, 34.71, 36.42],
        "netprofit_margin": [47.92, 49.15, 49.64, 49.51, 49.60],
        "debt_to_assets": [23.20, 22.85, 17.94, 14.92, 14.08],
        "n_cashflow_act": [64029000000.0, 66690000000.0, 66593000000.0,
                           92460000000.0, 96900000000.0],
        "total_assets": [255160000000.0, 275060000000.0, 301980000000.0,
                         338640000000.0, 361000000000.0],
        "equity_attr_p": [190510000000.0, 209030000000.0, 237940000000.0,
                          273420000000.0, 291070000000.0],
        "basic_eps": [41.76, 49.93, 59.49, 68.64, 73.11],
        "or_yoy": [11.71, 16.51, 18.03, 15.66, 6.36],
        "netprofit_yoy": [12.33, 19.54, 19.16, 15.38, 6.55],
        "net_after_nr_lp_correct": [52400000000.0, 62700000000.0,
                                    74300000000.0, 86000000000.0,
                                    91500000000.0],
        "operate_profit": [68500000000.0, 81700000000.0, 97200000000.0,
                           112000000000.0, 119000000000.0],
        "invent": [33300000000.0, 35600000000.0, 39500000000.0,
                   43200000000.0, 47000000000.0],
    }
    # 组织成 {metrics元组: [行...]} — 行含该批全部 bare 列
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
    return table


class FakeRegistry:
    """仅响应 query_financials (按 metrics 元组查表), 记录调用供断言"""

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
                    {"biz_type": "P", "bz_item": "茅台酒", "bz_sales": 145800000000.0,
                     "sales_share_pct": 83.9, "gross_margin_pct": 94.3},
                    {"biz_type": "P", "bz_item": "系列酒", "bz_sales": 24500000000.0,
                     "sales_share_pct": 14.1, "gross_margin_pct": 79.8},
                    {"biz_type": "P", "bz_item": "其他业务", "bz_sales": 3400000000.0,
                     "sales_share_pct": 2.0, "gross_margin_pct": None},
                    {"biz_type": "D", "bz_item": "国内", "bz_sales": 170000000000.0,
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
            # 观点/判断层板块 — 必须被排除
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


# ---------- 分片结构 ----------

def test_shard_kind_and_volume(shard):
    assert shard.kind == "tushare"
    assert shard.ts_code == "600519.SH"
    assert len(shard.facts) >= 60
    assert shard.views == []          # 纯事实层, 无观点
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
    # fake 数据指标全有值 → 无缺失记录
    assert shard.meta["coverage"]["metrics_missing_data"] == []


def test_missing_metric_recorded_not_fabricated(tmp_path, fake_profile):
    """指标全期无值 → 跳过并在 meta 记录, 不造数"""
    table = _extra_rows()
    for rows in table.values():                    # 抹掉 roe 列
        for r in rows:
            r["roe"] = None
    reg = FakeRegistry(table)
    s = collect_tushare_facts("贵州茅台", years=5, out_dir=tmp_path,
                              profile=fake_profile, registry=reg)
    assert not any(f.eid.startswith("T:tushare.roe.") for f in s.facts)
    assert "roe" in s.meta["coverage"]["metrics_missing_data"]


# ---------- 换算与口径 ----------

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
    # 2024 同年有中报行, 必须取年报行 (1741.43 亿而非 819 亿)
    assert by_eid["T:tushare.revenue.2024A"].value == pytest.approx(1741.43, abs=0.01)
    # 2026 只有中报行 → H1 标签 (累计口径, 不换算成年度)
    assert "T:tushare.revenue.2026H1" in by_eid
    assert "中报" in by_eid["T:tushare.revenue.2026H1"].content


def test_timeseries_clipped_to_years(shard):
    """深历史 (2014) 必须被截断; 保留最近 5 个年报年度 + 当年进行中期"""
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
    assert len(mb) == 3                      # 只取产品维度 3 条, 地区维度剔除
    assert not any("国内" in f.content for f in mb)
    first = {f.eid: f for f in mb}["T:profile.mainbiz.2025A.1"]
    assert "茅台酒" in first.content
    assert first.value == pytest.approx(1458.0, abs=0.01)
    assert "83.9" in first.content


# ---------- 观点层排除 ----------

def test_opinion_sections_excluded(shard):
    joined = " ".join(f.content for f in shard.facts)
    for kw in ("预测", "评级", "买入", "研报"):
        assert kw not in joined, f"事实层混入观点关键词「{kw}」"


# ---------- 落盘 round-trip ----------

def test_save_and_load_roundtrip(shard, tmp_path):
    path = tmp_path / "facts_tushare.json"
    assert path.exists()
    loaded = load_shard(path)
    assert loaded.kind == "tushare"
    assert len(loaded.facts) == len(shard.facts)
    assert loaded.facts[0].eid == shard.facts[0].eid
    assert shard.meta["path"] == str(path)


def test_fetch_batches_called(tmp_path, fake_profile):
    reg = FakeRegistry(_extra_rows())
    collect_tushare_facts("贵州茅台", years=5, out_dir=tmp_path,
                          profile=fake_profile, registry=reg)
    fin_calls = [c for c in reg.calls if c[0] == "query_financials"]
    assert len(fin_calls) == len(_EXTRA_BATCHES)
    assert all("最近5年" in c[1]["time"] for c in fin_calls)
