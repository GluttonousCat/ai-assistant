# -*- encoding: utf-8 -*-
"""T04 研报观点源测试 — 纯离线 (不连 LLM/DB): org 归一 / vid 生成 / 分类校验 /
单篇 View 构建 / 问答兜底解析 / 编排层端到端 (FakeRegistry 替身)。"""
from pathlib import Path

import pytest

from skills.research.contracts import load_shard, validate_shard
from skills.research.sources import report_source as rs
from skills.research.sources.report_source import (
    build_forecast_view,
    build_views_from_report,
    finalize_vids,
    norm_period,
    parse_qa_views,
    validate_views,
)


# ---------- fixture ----------

def _detail(rid=101, org="Goldman Sachs", date="2026-09-02", **analysis):
    full = {
        "ts_codes": ["600519.SH"], "company_names": ["贵州茅台"],
        "core_view": "高端白酒需求韧性强, 直渠改革红利释放。",
        "key_points": ["批价回落但动销稳健", "直渠占比提升", "i茅台贡献增量",
                       "分红率有望提升", "第五条应被截断"],
        "rating": "买入",
        "forecasts": [
            {"metric": "net_profit", "period": "2026E", "value": 862.0,
             "unit": "亿元", "raw_text": "预计2026年净利润862亿元"},
            {"metric": "target_price", "period": "NA", "value": 1850.0,
             "unit": "元", "raw_text": "12个月目标价1850元"},
            {"metric": "revenue", "period": "", "value": None, "unit": "",
             "raw_text": ""},  # 口径不全 -> 丢弃
        ],
        "risks": ["需求不及预期", "批价大幅波动", "第三条风险应被截断"],
    }
    full.update(analysis)
    return {"report_id": rid, "org": org, "publish_date": date, "analysis": full}


BRIEF_101 = {"report_id": 101, "org": "Goldman Sachs", "publish_date": "2026-09-02",
             "rating": "买入"}


# ---------- org 归一 ----------

class TestOrgNormalize:
    def test_english_bank_to_cn(self):
        views = build_views_from_report(_detail(), brief=BRIEF_101)
        assert views and all(v.org == "高盛" for v in views)

    def test_cn_name_unchanged(self):
        views = build_views_from_report(_detail(org="中信证券"),
                                        brief={"report_id": 101, "org": "中信证券",
                                               "publish_date": "2026-09-10"})
        assert all(v.org == "中信证券" for v in views)

    def test_forecast_row_org_normalized(self):
        v = build_forecast_view({"report_id": 9, "org_name": "Morgan Stanley",
                                 "publish_date": "2026-08-20", "forecast_type": "net_profit",
                                 "forecast_period": "2026E", "forecast_value": 900.1,
                                 "forecast_unit": "亿元"})
        assert v is not None and v.org == "摩根士丹利"

    def test_qa_org_normalized(self):
        views = parse_qa_views("核心观点: 需求稳固", 1, "JPMorgan", "2026-09-01")
        assert views and views[0].org == "摩根大通"


# ---------- vid 生成 ----------

class TestVidGeneration:
    def test_format_and_uniqueness(self):
        a = build_views_from_report(_detail(rid=101), brief=BRIEF_101)
        b = build_views_from_report(_detail(rid=202, org="中信证券"),
                                    brief={"report_id": 202, "org": "中信证券",
                                           "publish_date": "2026-09-10"})
        finalize_vids(a + b)
        vids = [v.vid for v in a + b]
        assert len(vids) == len(set(vids))
        assert all(rs._VID_RE.match(x) for x in vids)

    def test_seq_per_report(self):
        a = build_views_from_report(_detail(rid=101), brief=BRIEF_101)
        b = build_views_from_report(_detail(rid=202, org="中信证券"),
                                    brief={"report_id": 202, "org": "中信证券",
                                           "publish_date": "2026-09-10"})
        finalize_vids(a + b)
        assert [v.vid for v in a] == [f"R:101.{i}" for i in range(1, len(a) + 1)]
        assert [v.vid for v in b] == [f"R:202.{i}" for i in range(1, len(b) + 1)]


# ---------- 分类与口径校验 ----------

class TestCategoryValidation:
    def test_valid_fixture_passes(self):
        views = finalize_vids(build_views_from_report(_detail(), brief=BRIEF_101))
        assert validate_views(views) == []
        cats = {v.category for v in views}
        assert cats == {"viewpoint", "forecast", "rating", "risk"}

    def test_unknown_category_rejected(self):
        v = build_views_from_report(_detail(), brief=BRIEF_101)[0]
        v.category = "opinion"
        assert any("category 非法" in p for p in validate_views([v]))

    def test_forecast_requires_period(self):
        views = finalize_vids(build_views_from_report(_detail(), brief=BRIEF_101))
        fc = next(v for v in views if v.category == "forecast")
        assert fc.period                      # forecast 必带期间
        fc.period = ""
        assert any("缺 period" in p for p in validate_views(views))

    def test_missing_org_rejected(self):
        v = build_views_from_report(_detail(), brief=BRIEF_101)[0]
        v.org = ""
        assert any("缺 org" in p for p in validate_views([v]))

    def test_duplicate_vid_rejected(self):
        vs = finalize_vids(build_views_from_report(_detail(), brief=BRIEF_101))
        vs[1].vid = vs[0].vid
        assert any("vid 重复" in p for p in validate_views(vs))

    def test_forecast_statement_carries_value_and_unit(self):
        vs = build_views_from_report(_detail(), brief=BRIEF_101)
        fc = next(v for v in vs if v.category == "forecast")
        assert "862" in fc.statement and "亿元" in fc.statement


# ---------- 单篇构建 ----------

class TestBuildViewsFromReport:
    def test_caps_and_dropped_and_order(self):
        views = build_views_from_report(_detail(), brief=BRIEF_101)
        by_cat = [v.category for v in views]
        # 顺序: core(1) -> key_points(截到4) -> 评级(1) -> 预测+目标价(2) -> 风险(截到2)
        assert by_cat == ["viewpoint"] + ["viewpoint"] * 4 + ["rating", "forecast",
                                                              "rating"] + ["risk"] * 2
        assert len(views) == 10
        assert all("截断" not in v.statement for v in views)

    def test_rating_wu_omitted(self):
        d = _detail()
        d["analysis"]["rating"] = "无"
        views = build_views_from_report(d, brief={**BRIEF_101, "rating": None})
        assert not any("评级为" in v.statement for v in views)

    def test_target_price_is_rating(self):
        views = build_views_from_report(_detail(), brief=BRIEF_101)
        tp = [v for v in views if "目标价" in v.statement]
        assert tp and all(v.category == "rating" and v.period == "" for v in tp)

    def test_brief_only_when_detail_fails(self):
        views = build_views_from_report(None, brief=BRIEF_101)
        assert [v.category for v in views] == ["rating"]
        assert views[0].org == "高盛"

    def test_no_date_no_view(self):
        views = build_views_from_report(_detail(date=""), brief={"report_id": 1})
        assert views == []

    def test_incomplete_forecast_dropped(self):
        views = build_views_from_report(_detail(), brief=BRIEF_101)
        assert not any(v.period == "" and v.category == "forecast" for v in views)


# ---------- 期间归一 ----------

class TestNormPeriod:
    def test_na_and_empty(self):
        assert norm_period("NA") == "" and norm_period("") == "" and norm_period("na") == ""

    def test_estimate_degrades_to_year(self):
        # 契约 valid_period 暂不支持 YYYYE -> 降级 YYYY (口径由 statement 保留)
        assert norm_period("2026E") in ("2026E", "2026")
        assert norm_period("2026e") in ("2026E", "2026")

    def test_plain_formats_pass(self):
        assert norm_period("2026") == "2026"
        assert norm_period("2026A") == "2026A"
        assert norm_period("2026H1") == "2026H1"
        assert norm_period("2026Q3") == "2026Q3"
        assert norm_period("2026-09-02") == "2026-09-02"


# ---------- 表行预测 ----------

class TestBuildForecastView:
    def _row(self, **kw):
        row = {"report_id": 9, "org_name": "高盛", "publish_date": "2026-08-20",
               "forecast_type": "net_profit", "forecast_period": "2026E",
               "forecast_value": 900.1, "forecast_unit": "亿元"}
        row.update(kw)
        return row

    def test_statement_carries_period_value_unit(self):
        v = build_forecast_view(self._row())
        assert v.category == "forecast" and v.period in ("2026E", "2026")
        assert "900.1" in v.statement and "亿元" in v.statement and "净利润" in v.statement

    def test_target_price_is_rating(self):
        v = build_forecast_view(self._row(forecast_type="target_price",
                                          forecast_period="NA", forecast_value=2000.0,
                                          forecast_unit="元"))
        assert v.category == "rating" and v.period == ""
        assert "2000" in v.statement and "元" in v.statement

    def test_missing_org_returns_none(self):
        assert build_forecast_view(self._row(org_name=None)) is None

    def test_missing_period_returns_none(self):
        assert build_forecast_view(self._row(forecast_period="")) is None

    def test_integer_value_no_trailing_zero(self):
        v = build_forecast_view(self._row(forecast_value=900.0))
        assert "900亿元" in v.statement


# ---------- 问答兜底解析 ----------

class TestParseQaViews:
    def test_labeled_lines_with_bullets(self):
        ans = ("核心观点: 白酒龙头基本面稳固\n- 评级: 增持\n2. 风险: 消费复苏低于预期\n"
               "评级: 未提及\n无关行不解析")
        views = parse_qa_views(ans, 5, "中信证券", "2026-09-10")
        assert [(v.category, v.statement) for v in views] == [
            ("viewpoint", "白酒龙头基本面稳固"),
            ("rating", "评级为「增持」"),
            ("risk", "消费复苏低于预期"),
        ]

    def test_pipe_forecast_line(self):
        views = parse_qa_views("盈利预测: 2026E|净利润|880|亿元", 5, "中信证券",
                               "2026-09-10")
        assert len(views) == 1
        v = views[0]
        assert v.category == "forecast" and v.period in ("2026E", "2026")
        assert "880" in v.statement and "亿元" in v.statement

    def test_prose_forecast_period_extracted(self):
        views = parse_qa_views("盈利预测: 预计2027年净利润将达950亿元", 5, "野村",
                               "2026-09-10")
        assert views[0].period == "2027"

    def test_max_views_cap(self):
        ans = "\n".join(f"风险: 风险{i}" for i in range(9))
        assert len(parse_qa_views(ans, 5, "野村", "2026-09-10", max_views=6)) == 6

    def test_no_org_no_views(self):
        assert parse_qa_views("核心观点: x", 5, "", "2026-09-10") == []
        assert parse_qa_views("核心观点: x", 5, "野村", "") == []


# ---------- 主标的判定 (非主标的报告加归属前缀) ----------

class TestSubjectPrefix:
    def test_subject_by_ts_codes(self):
        assert rs._context_prefix(_detail(), BRIEF_101, "600519.SH", "贵州茅台") == ""

    def test_non_subject_prefixed_with_company_name(self):
        d = _detail()
        d["analysis"]["ts_codes"] = ["000858.SZ"]
        d["analysis"]["company_names"] = ["五粮液"]
        assert rs._context_prefix(d, BRIEF_101, "600519.SH", "贵州茅台") == "【五粮液】"
        views = build_views_from_report(d, brief=BRIEF_101, ts_code="600519.SH",
                                        context_prefix="【五粮液】")
        assert all(v.statement.startswith("【五粮液】") for v in views)

    def test_non_subject_without_company_name(self):
        d = _detail()
        d["analysis"]["ts_codes"] = ["000858.SZ"]
        d["analysis"]["company_names"] = []
        assert rs._context_prefix(d, BRIEF_101, "600519.SH", "贵州茅台") == "【关联提及】"

    def test_subject_by_title_when_ts_codes_empty(self):
        d = _detail()
        d["analysis"]["ts_codes"] = []
        brief = {**BRIEF_101, "title": "贵州茅台: 直渠改革跟踪"}
        assert rs._context_prefix(d, brief, "600519.SH", "贵州茅台") == ""

    def test_subject_by_brief_target_when_ts_codes_empty(self):
        d = _detail()
        d["analysis"]["ts_codes"] = []
        brief = {**BRIEF_101, "target": "贵州茅台"}
        assert rs._context_prefix(d, brief, "600519.SH", "贵州茅台") == ""

    def test_undetermined_gets_generic_prefix(self):
        d = _detail()
        d["analysis"]["ts_codes"] = []
        d["analysis"]["company_names"] = []
        assert rs._context_prefix(d, BRIEF_101, "600519.SH", "贵州茅台") == "【关联提及】"


# ---------- 编排层端到端 (FakeRegistry) ----------

class FakeRegistry:
    """MCP 注册表替身: 返回固定信封, 记录调用参数"""

    def __init__(self):
        self.calls = []

    def call(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        if name == "get_forecasts":
            return {"ok": True, "data": self.forecasts_data()}
        if name == "search_reports":
            return {"ok": True, "data": {"count": 2, "reports": [
                {"report_id": 101, "org": "Goldman Sachs",
                 "publish_date": "2026-09-02", "rating": "买入"},
                {"report_id": 102, "org": "中信证券",
                 "publish_date": "2026-09-10", "rating": None},
            ]}}
        if name == "read_report":
            rid = int(arguments["report_id"])
            if arguments.get("question"):
                return {"ok": True, "data": {"answer": (
                    "核心观点: 白酒龙头基本面稳固\n评级: 增持\n"
                    "盈利预测: 2026E|净利润|880|亿元\n风险: 消费复苏低于预期\n"
                    "目标价: 2000元")}}
            if rid == 101:
                return {"ok": True, "data": _detail()}
            if rid == 102:
                return {"ok": True, "data": {
                    "report_id": 102, "org": "中信证券",
                    "publish_date": "2026-09-10",
                    "analysis": {"ts_codes": ["600519.SH"],
                                 "company_names": ["贵州茅台"],
                                 "core_view": "", "key_points": []}}}
            return {"ok": False, "tool": "read_report", "error": "研报不存在"}
        return {"ok": False, "tool": name, "error": "未知工具"}

    @staticmethod
    def forecasts_data():
        return {
            "stock": {"ts_code": "600519.SH", "name": "贵州茅台"},
            "divergence_stats": [{"forecast_type": "net_profit",
                                  "forecast_period": "2026E", "cv": 0.08}],
            "recent_forecasts": [
                # 清单外旧报告的新预测 -> 应补入 (机构归一)
                {"forecast_type": "net_profit", "forecast_period": "2026E",
                 "forecast_value": 900.1, "forecast_unit": "亿元",
                 "org_name": "Morgan Stanley", "publish_date": "2026-08-20",
                 "report_id": 900, "title": "旧报告", "confidence": None},
                # 与 101 篇内 analysis 预测同篇同期间同指标 -> 不重复补
                {"forecast_type": "net_profit", "forecast_period": "2026E",
                 "forecast_value": 862.0, "forecast_unit": "亿元",
                 "org_name": "Goldman Sachs", "publish_date": "2026-09-02",
                 "report_id": 101, "title": "重复", "confidence": None},
                # 缺机构 -> 丢弃
                {"forecast_type": "revenue", "forecast_period": "2027E",
                 "forecast_value": 2100.0, "forecast_unit": "亿元",
                 "org_name": None, "publish_date": "2026-08-01",
                 "report_id": 901, "title": "无机构", "confidence": None},
            ],
            "cv_hint": "CV>0.3 分歧较大",
        }


class TestCollectOffline:
    def _collect(self, monkeypatch, tmp_path, **kw):
        fake = FakeRegistry()
        monkeypatch.setattr(rs, "_mcp", lambda: fake)
        shard = rs.collect_report_views("贵州茅台", out_dir=tmp_path, **kw)
        return shard, fake

    def test_end_to_end_shard_valid(self, monkeypatch, tmp_path):
        shard, fake = self._collect(monkeypatch, tmp_path)
        # 101 篇 10 条 + 102 问答 5 条 + 表行补 1 条 = 16; 重复/无机构表行不补
        assert len(shard.views) == 16
        assert shard.meta["orgs"] == ["中信证券", "摩根士丹利", "高盛"]
        assert validate_shard(shard) == []          # T01 契约校验器
        assert validate_views(shard.views) == []    # T04 语义自检
        assert shard.meta["qa_fallback_used"] == 1
        # 落盘可回读
        path = tmp_path / "views_reports.json"
        assert path.exists()
        loaded = load_shard(path)
        assert len(loaded.views) == len(shard.views)
        assert {v.vid for v in loaded.views} == {v.vid for v in shard.views}

    def test_mcp_tool_routing(self, monkeypatch, tmp_path):
        shard, fake = self._collect(monkeypatch, tmp_path)
        names = [c[0] for c in fake.calls]
        assert names[0] == "get_forecasts"          # 解析标的+预测时序
        assert "search_reports" in names
        assert ("search_reports",
                {"stock": "贵州茅台", "days": 365, "limit": 30}) in fake.calls
        qa_calls = [c for c in fake.calls
                    if c[0] == "read_report" and c[1].get("question")]
        assert len(qa_calls) == 1 and qa_calls[0][1]["report_id"] == 102

    def test_qa_fallback_disabled(self, monkeypatch, tmp_path):
        shard, _ = self._collect(monkeypatch, tmp_path, qa_fallback=0)
        assert shard.meta["qa_fallback_used"] == 0
        assert not any(v.report_id == 102 for v in shard.views)
        assert len(shard.views) == 11               # 仅 101 篇 10 条 + 表行 1 条

    def test_unresolvable_stock_raises(self, monkeypatch, tmp_path):
        class BadReg(FakeRegistry):
            def call(self, name, arguments=None):
                return {"ok": False, "tool": name, "error": "未识别到股票实体"}
        monkeypatch.setattr(rs, "_mcp", lambda: BadReg())
        with pytest.raises(ValueError):
            rs.collect_report_views("不存在的股票", out_dir=tmp_path)
