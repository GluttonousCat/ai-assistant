import json
from zipfile import ZipFile

import pytest

from skills.report.document import Document, Segment, extraction_batches, load_document
from skills.report.extraction import analyze_document


def test_insights_use_matching_page_and_condition_with_repeated_evidence():
    quote = "We expect margins to rise if\n   utilization exceeds 80%."
    source = {"segment": 1, "page": 42, "quote": "We expect margins to rise if utilization exceeds 80%."}
    note = {
        "kind": "driver", "statement": "利用率超过80%时预计利润率上升。",
        "source": source,
    }
    insight = {**note, "insight_type": "watchpoints", "indicator": "产能利用率", "condition": "高于80%"}

    class LLM:
        def invoke(self, prompt):
            return json.dumps({"core_view": "利润率依赖产能利用率。", "evidence": [
                note, insight, insight,
                {**insight, "condition": "高于99%"},
                {**insight, "statement": "90%利用率。"},
                {**insight, "horizon": "2027年"},
                {**insight, "source": {**source, "segment": 3}},
            ]})

    document = Document([Segment(1, 2, quote)], "pdf_text", 2, (1,))
    result = list(analyze_document(document, "经营预测", LLM()))[-1]["data"]
    assert result["research_insights"]["watchpoints"] == [{
        "statement": note["statement"], "indicator": "产能利用率", "condition": "高于80%",
        "source": {"segment": 1, "page": 2, "quote": quote},
    }]
    assert len(result["evidence"]) == 1
    assert result["coverage"]["rejected_insights"] == 2
    assert result["coverage"]["rejected_evidence"] == 2


def test_oversized_report_preserves_endpoints_and_prioritizes_forecast_changes(monkeypatch):
    monkeypatch.setattr("skills.report.document._CHUNK_CHARS", 32)
    segments = [
        Segment(i, i, "较前次盈利预测调整；估值方法与核心假设" if i == 3 else "新闻回顾" * 10)
        for i in range(1, 25)
    ]
    batches = extraction_batches(Document(segments, "pdf_text", 24))
    numbers = [s.number for batch in batches for s in batch]
    assert len(numbers) == 12
    assert numbers == sorted(set(numbers))
    assert numbers[0] == 1 and numbers[-1] == 24 and 3 in numbers


def test_truncated_docx_recovers_original_forecast_table_in_context(tmp_path):
    source = tmp_path / "case.docx"
    with ZipFile(source, "w") as f:
        f.writestr("word/document.xml", """
           <w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
             <w:body><w:p><w:r><w:t>基准情景假设</w:t></w:r></w:p>
             <w:tbl><w:tr><w:tc><w:p><w:r><w:t>利用率</w:t></w:r></w:p></w:tc>
             <w:tc><w:p><w:r><w:t>80%</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
             <w:p><w:r><w:t>悲观情景假设</w:t></w:r></w:p></w:body>
           </w:document>""".encode())
    doc = load_document({"file_path": str(source), "content_chars": 12, "content_text": "基准情景"})
    assert doc.source == "local_file"
    assert doc.segments[0].text == "基准情景假设\n利用率 | 80%\n悲观情景假设"
    assert doc.segments[0].page is None


def test_legacy_pending_attachment_discards_its_topic_commentary(tmp_path):
    row = {"file_path": str(tmp_path / "missing.pdf"), "source": "zsxq", "extraction_status": "pending",
           "content_text": "这段正文是发帖者的转述，来源不是附件。", "content_chars": 21}
    assert load_document(row).segments == []
    path = tmp_path / "recovered.txt"
    path.write_text("恢复的附件内容。", encoding="utf-8")
    doc = load_document({**row, "file_path": str(path)})
    assert doc.segments[0].text == "恢复的附件内容。"


def test_extracted_zsxq_scan_cannot_reuse_unsourced_posting_text(tmp_path, monkeypatch):
    from types import SimpleNamespace

    path = tmp_path / "scan.pdf"
    path.touch()
    monkeypatch.setattr("skills.report.document.get_extractor", lambda: SimpleNamespace(pdf_pages=lambda _: [""]))
    row = {"file_path": str(path), "file_name": "scan.pdf", "source": "zsxq", "extraction_status": "extracted",
           "content_text": "历史合并覆盖了状态，无法证明这段文字来自原件。"}
    assert load_document(row).segments == []
    assert load_document({**row, "file_path": None}).segments == []
    assert load_document({**row, "source": "upload"}).segments


@pytest.mark.parametrize("repair_fails", [False, True])
def test_relocates_only_known_claims_once_and_keeps_verified_results(monkeypatch, repair_fails):
    monkeypatch.setattr("skills.report.document._CHUNK_CHARS", 40)
    quote = "产能利用率达到80%时，利润率预计改善。"
    doc = Document([Segment(i, i, quote + "补充材料" * 20) for i in (1, 2)], "pdf_text", 2)
    repair_calls = []

    class LLM:
        def invoke(self, prompt):
            if "逐条核对下方已有陈述" in prompt:
                repair_calls.append(prompt)
                if repair_fails:
                    raise TimeoutError
                return json.dumps({"sources": [
                    {"id": i, "statement": "模型新加的说法不能保存", "source": {"segment": 1, "quote": quote}}
                    for i in (2, 3, 40)
                ]})
            if "合成一份" in prompt:
                return '{"core_view":"利润率改善"}'
            segment = 1 if "PDF 第 1 页" in prompt else 2
            return json.dumps({"evidence": [
                {"kind": "driver", "statement": s, "source": {"segment": segment, "quote": q}}
                for s, q in [("预计利润率改善。", quote), ("利用率达到80%。", "找不到先前摘录。"),
                             ("利用率达到90%。", quote), ("利润率受到关注。", "摘录无法定位。")]
            ]})

    result = list(analyze_document(doc, "产能利用率", LLM()))[-1]["data"]
    assert len(repair_calls) == result["coverage"]["evidence_repair_attempts"] == 1
    assert result["coverage"]["recovered_evidence"] == (0 if repair_fails else 1)
    statements = [n["statement"] for n in result["evidence"]]
    assert statements == (["预计利润率改善。"] if repair_fails else ["预计利润率改善。", "利用率达到80%。"])
