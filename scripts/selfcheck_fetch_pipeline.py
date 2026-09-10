# -*- encoding: utf-8 -*-
"""下载流水线 _process_one_report / sync.run 返回值 mock 自检 (不连 PG / 不调 LLM / 不下载)"""
import sys
sys.path.insert(0, '.')

from unittest import mock

import scripts.daily_fetch_zxsq as fetch_mod
import scripts.sync_zxsq_to_pg as sync_mod

calls = {"meta": [], "extract": [], "merge": 0}


class FakeSync:
    def __init__(self, group_id, limit=None): pass
    def run(self): return [101, 102]  # 101=pdf, 102=txt(话题文本)


class FakePg:
    def __init__(self): pass
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def fetch_all(self, sql, args=None):
        return [{"report_id": 101, "file_name": "高盛-半导体月报.pdf"},
                {"report_id": 102, "file_name": "topic_88_解读.txt"}]


class FakeSkill:
    def __call__(self, ctx):
        assert ctx.params["mode"] == "extract" and ctx.params["limit"] == 1
        assert ctx.params["report_id"] == 101, "只应处理 PDF 那篇"
        calls["extract"].append(ctx.params["report_id"])
        ctx.result = {"summary": "ok"}
        return ctx


def fake_meta(rid, force=False):
    calls["meta"].append(rid)
    return True


def fake_merge():
    calls["merge"] += 1
    return {}


with mock.patch.object(sync_mod, "ZSXQ2PGSync", FakeSync), \
     mock.patch("storage.pg.PgClient", FakePg), \
     mock.patch("tools.finance.report_meta_analysis.analyze_report_meta", fake_meta), \
     mock.patch("skills.report.skill.ReportSkill", FakeSkill), \
     mock.patch("scripts.merge_report_duplicates.merge", fake_merge):
    # 用例 1: 有新文件 -> 仅 PDF 逐篇 元数据+深度提取
    r = fetch_mod._process_one_report("51288148188224")
    print("case1:", r, calls)
    assert r == "ok+extracted 1", r
    assert calls["meta"] == [101] and calls["extract"] == [101], calls
    assert calls["merge"] == 1

    # 用例 2: 无新文件 -> noop + 兜底 analyze_pending(5)
    class FakeSyncEmpty(FakeSync):
        def run(self): return []
    pend = {"n": 0}
    with mock.patch.object(sync_mod, "ZSXQ2PGSync", FakeSyncEmpty), \
         mock.patch("tools.finance.report_meta_analysis.analyze_pending",
                    lambda limit=5: pend.update(n=pend["n"] + 1) or 1):
        r2 = fetch_mod._process_one_report("51288148188224")
        print("case2:", r2, pend)
        assert r2 == "noop" and pend["n"] == 1

print("PIPELINE MOCK PASSED")
