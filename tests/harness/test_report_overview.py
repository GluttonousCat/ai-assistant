import asyncio
import json

import pytest

from harness import report_overview as overview


def row(rid, term, copy_key=None):
    return {"report_id": rid, "matched_keyword": term, "topic_rank": 0, "org": str(rid),
            "material_type": "original", "copy_key": copy_key}


QUOTE = "我们预计库存仍会持续下降。"


@pytest.mark.parametrize("missing", [False, True])
def test_overview_reads_one_batch_and_only_supplements_missing_evidence(monkeypatch, missing):
    calls = []
    prompts = []

    class Model:
        def invoke(self, prompt):
            prompts.append(prompt)
            if len(prompts) == 1:
                return json.dumps({"topics": [{"keyword": "金", "alternate": "黄金"},
                                              {"keyword": "铝", "alternate": "电解铝"}], "days": 7})
            claims = [{"report_id": 10, "topic": "金"}, {"report_id": 20, "topic": "铝"}]
            if missing and len(prompts) == 2:
                # The PDF had text, but the model cited an unrequested topic.
                claims[1]["topic"] = "原油"
            if len(prompts) == 3:
                claims = [{"report_id": 21, "topic": "铝"}]
            return json.dumps({"claims": [
                {**c, "statement": QUOTE, "attribution": "broker", "stance": "expectation",
                 "source": {"segment": 1, "quote": QUOTE}} for c in claims]})

    async def execute(requests):
        calls.append(requests)
        if requests[0][0] == "search_reports":
            keys = requests[0][1]["keywords"]
            rows = ([row(10, "金", "same"), row(11, "金", "same"), row(20, "铝")]
                    if keys == ["金", "铝"] else [row(21, "电解铝")])
            return [{"ok": True, "data": {"count": len(rows), "reports": rows}}]
        return [{"ok": True, "data": {
            "report_id": args["report_id"], "org": "机构", "title": "报告", "material_type": "original",
            "page_count": 8, "pages_with_text": 1, "body_status": "partial",
            "passages": [{"number": 1, "page": 3, "text": QUOTE}]}} for _, args in requests]

    monkeypatch.setattr(overview, "execute_batch", execute)
    holder = {}
    async def run():
        return [event async for event in overview.run_overview(
            Model(), [], "金和铝的综述", holder, lambda key, data: data)]
    events = asyncio.run(run())
    assert [a["report_id"] for _, a in calls[1]] == [10, 20]
    assert calls[0][0][1]["days"] == 7
    if missing:
        assert calls[2][0][1]["keywords"] == ["电解铝"]
        assert [a["report_id"] for _, a in calls[3]] == [21]
    assert len(calls) == (4 if missing else 2)
    assert events[-1]["model_calls"] == len(prompts) == (3 if missing else 2)
    assert not events[-1]["uncovered_topics"]
    assert "研报 #10" in holder["final_text"] and "PDF 第3页" in holder["final_text"]
    assert "已识别 1/8 页" in holder["final_text"]


def test_overview_uses_unread_reserve_before_issuing_another_search(monkeypatch):
    history = []
    class Model:
        def invoke(self, _):
            return '{"topics":[{"keyword":"黄金","alternate":"贵金属"}]}'

    async def execute(calls):
        history.append(calls)
        if calls[0][0] == "search_reports":
            return [{"ok": True, "data": {"reports": [row(n, "黄金") for n in range(1, 9)]}}]
        return [{"ok": False, "error": "读取失败"} for _ in calls]

    monkeypatch.setattr(overview, "execute_batch", execute)
    holder = {}
    async def run():
        return [data async for data in overview.run_overview(
            Model(), [], "黄金综述", holder, lambda _, data: data)]
    stages = asyncio.run(run())
    assert stages[-1]["search_calls"] == 1 and stages[-1]["read_batches"] == 2
    assert stages[-1]["model_calls"] == 1 and stages[-1]["uncovered_topics"] == ["黄金"]
    assert [a["report_id"] for _, a in history[-1]] == [7]
    assert "未成功读取" in holder["final_text"]


def test_overview_only_handles_cross_report_questions():
    assert overview.is_report_overview("最近大宗的观点都有哪些，特别是看看贵金属。", "report")
    assert overview.is_report_overview("帮我做个半导体研报综述", "report")
    for text in ("这篇研报的观点有哪些", "列出最近有哪些黄金研报", "对比 #123 与 #456", "查盈利预测均值"):
        assert not overview.is_report_overview(text, "report")


def test_indexed_quote_must_match_the_current_original_and_page():
    from skills.report.answers import indexed_original_quote
    from skills.report.document import Document, Segment

    doc = Document([Segment(1, 3, QUOTE)], "pdf_ocr", 22)
    source = {"segment": 1, "page": 3, "quote": QUOTE}
    analysis = {"material_type": "original", "forecast_excerpts": [source]}
    assert indexed_original_quote(analysis, doc) == source
    assert not indexed_original_quote({**analysis, "material_type": "commentary"}, doc)
    assert not indexed_original_quote({**analysis, "forecast_excerpts": [{**source, "page": 5}]}, doc)
    assert not indexed_original_quote(analysis, Document([Segment(1, 3, "已更换材料")], "pdf_text", 3))
