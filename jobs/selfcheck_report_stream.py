# -*- encoding: utf-8 -*-
"""extract_stream event-stream mock self-check (no PG connection / no LLM calls)."""
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
    def execute(self, sql, args=None):
        self.sqls.append((sql, args))
        return 1  # rowcount (0 = the record was already deleted by merge)


class FakeLLM:
    def stream(self, prompt, **kw):
        # The thinking toggle is injected uniformly by LLMClient._default_kwargs and
        # never passed by business code — kw must be empty
        assert not kw, f"业务层不应传思考参数: {kw}"
        for i in range(0, len(FAKE_JSON), 40):
            yield FAKE_JSON[i:i + 40]
    def invoke(self, prompt, **kw):
        assert not kw, f"业务层不应传思考参数: {kw}"
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


# Case 1: normal chain (body ready -> streaming extraction -> parse -> persist -> done)
events, pg = run_case("normal")
seq = [e["type"] for e in events]
assert seq[0] == "stage" and events[-1]["type"] == "done", seq
assert "delta" in seq and seq.count("data") == 1 and "error" not in seq, seq
data_ev = [e for e in events if e["type"] == "data"][0]["data"]
assert data_ev["forecast_count"] == 1 and data_ev["market"] == "A股", data_ev
# Forecasts must be persisted: DELETE + INSERT fin.report_forecast
sqls = " || ".join(s for s, _ in pg.sqls)
assert "DELETE FROM fin.report_forecast" in sqls, "forecast DELETE 未执行"
assert "INSERT INTO fin.report_forecast" in sqls, "forecast INSERT 未执行"
assert "analysis_status='done'" in sqls, "analysis_status 未置 done"
joined = "".join(e.get("text", "") for e in events if e["type"] == "delta")
assert joined == FAKE_JSON, "delta 流拼接不完整"

# Case 2: image-only PDF -> OCR handoff notice + error (not marked failed)
events2, pg2 = run_case(
    "image-pdf",
    row_over={"content_chars": 0, "content_text": None, "file_path": "D:/x.pdf"},
    patch_vision=lambda row: False,
)
seq2 = [e["type"] for e in events2]
assert seq2[-1] == "error" and "delta" not in seq2, seq2  # done is re-emitted by the API layer
assert not any("analysis_status='failed'" in s for s, _ in pg2.sqls), "OCR 排队不应标 failed"

# Case 3: record deleted by merge during extraction (UPDATE rowcount=0) ->
# discard the result, do not insert forecasts
skill3 = mod.ReportSkill()
pg3 = FakePg()
orig_exec = pg3.execute
state = {"dropped": False}
def exec3(sql, args=None):
    pg3.sqls.append((sql, args))
    if sql.startswith("UPDATE fin.report_meta") and not state["dropped"]:
        state["dropped"] = True
        return 0  # simulate the record having been deleted
    return 1
pg3.execute = exec3
mod.PgClient = lambda: pg3
skill3._ensure_columns = staticmethod(lambda p: None)
skill3._extract_llm = staticmethod(lambda: FakeLLM())
summary = skill3._apply_extract_result(pg3, {"report_id": 9, "title": "x",
                                             "content_text": "y", "ts_code": None,
                                             "symbols": None, "report_type": None,
                                             "org_name": None}, __import__("json").loads(FAKE_JSON))
assert summary is None, summary
assert not any("INSERT INTO fin.report_forecast" in s for s, _ in pg3.sqls), "被删记录不应插 forecast"
print("--- dropped-by-merge ---\n  OK: 返回 None 且无 forecast INSERT")

print("\nALL CASES PASSED")
