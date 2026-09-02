# -*- encoding: utf-8 -*-
"""extract_stream 事件流 mock 自检 (不连 PG / 不调 LLM)"""
import sys, types
sys.path.insert(0, '.')

import skills.report.skill as mod

FAKE_JSON = ('{"market": "A股", "ts_codes": ["600519.SH"], "symbols": [], '
             '"company_names": ["贵州茅台"], "commodities": [], "report_type": "深度", '
             '"rating": "买入", "org_name": "中信证券", "author": "张三", '
             '"publish_date": "2026-08-20", "core_view": "业绩稳健", '
             '"key_points": ["高端酒占比提升", "现金流充沛"], '
             '"forecasts": [{"symbol": "600519.SH", "metric": "net_profit", '
             '"period": "2026E", "value": 900.0, "unit": "亿元", '
             '"raw_text": "预计2026年净利900亿"}], '
             '"risks": ["消费疲软"], "tags": {"targets": ["贵州茅台"], '
             '"industries": ["白酒"], "regions": ["中国大陆"], '
             '"commodities": [], "themes": ["消费复苏"]}}')


class FakeCursor:
    def __init__(self, sqls): self.sqls = sqls
    def execute(self, sql, args=None): self.sqls.append((sql, args))
    def fetchall(self): return []
    def fetchone(self): return None
    def close(self): pass


class FakePg:
    def __init__(self): self.sqls = []
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def fetch_one(self, sql, args=None):
        if 'report_id=%s' in sql:
            return {"report_id": 1, "title": "贵州茅台深度",
                    "content_text": "正文" * 500, "content_chars": 1000,
                    "ts_code": None, "report_type": None, "author": None,
                    "org_name": None, "publish_date": None, "market": None,
                    "symbols": None, "file_path": None,
                    "extraction_status": "extracted"}
        return None
    def fetch_all(self, sql, args=None): return []
    def execute(self, sql, args=None): self.sqls.append((sql, args))


class FakeLLM:
    def stream(self, prompt, **kw):
        assert kw.get("extra_body") == {"enable_thinking": False}, kw
        for i in range(0, len(FAKE_JSON), 40):
            yield FAKE_JSON[i:i + 40]
    def invoke(self, prompt, **kw):
        assert kw.get("extra_body") == {"enable_thinking": False}, kw
        return FAKE_JSON


def run_case(name, row_over=None, patch_vision=None):
    skill = mod.ReportSkill()
    pg = FakePg()
    mod.PgClient = lambda: pg
    skill._ensure_columns = staticmethod(lambda p: None)
    skill._extract_llm = staticmethod(lambda: FakeLLM())
    if patch_vision is not None:
        skill._vision_backfill = patch_vision
    if row_over:
        orig = pg.fetch_one
        def fetch_one(sql, args=None):
            r = orig(sql, args)
            if r and 'content_text' in sql:
                r.update(row_over)
            return r
        pg.fetch_one = fetch_one

    events = list(skill.extract_stream(1))
    print(f"--- {name} ---")
    for ev in events:
        t = ev["type"]
        if t == "delta":
            print(f"  delta ... ({len(ev['text'])} chars)")
        else:
            info = {k: v for k, v in ev.items() if k != "type"}
            if "data" in info:
                d = info.pop("data")
                info["data.forecast_count"] = d.get("forecast_count")
                info["data.market"] = d.get("market")
            print(f"  {t}: {info}")
    return events, pg


# 用例 1: 正常链路 (正文就绪 -> 流式提取 -> 解析 -> 入库 -> done)
events, pg = run_case("normal")
seq = [e["type"] for e in events]
assert seq[0] == "stage" and events[-1]["type"] == "done", seq
assert "delta" in seq and seq.count("data") == 1 and "error" not in seq, seq
data_ev = [e for e in events if e["type"] == "data"][0]["data"]
assert data_ev["forecast_count"] == 1 and data_ev["market"] == "A股", data_ev
# 盈利预测必须入库: DELETE + INSERT fin.report_forecast
sqls = " || ".join(s for s, _ in pg.sqls)
assert "DELETE FROM fin.report_forecast" in sqls, "forecast DELETE 未执行"
assert "INSERT INTO fin.report_forecast" in sqls, "forecast INSERT 未执行"
assert "analysis_status='done'" in sqls, "analysis_status 未置 done"
joined = "".join(e.get("text", "") for e in events if e["type"] == "delta")
assert joined == FAKE_JSON, "delta 流拼接不完整"

# 用例 2: 图片型 PDF -> 转 OCR 提示 + error (不标 failed)
events2, pg2 = run_case(
    "image-pdf",
    row_over={"content_chars": 0, "content_text": None, "file_path": "D:/x.pdf"},
    patch_vision=lambda row: False,
)
seq2 = [e["type"] for e in events2]
assert seq2[-1] == "error" and "delta" not in seq2, seq2  # done 由 API 层补发
assert not any("analysis_status='failed'" in s for s, _ in pg2.sqls), "OCR 排队不应标 failed"

print("\nALL CASES PASSED")
