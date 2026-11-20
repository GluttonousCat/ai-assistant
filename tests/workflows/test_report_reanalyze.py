import json

import pytest

from workflows.reports import reanalyze


def test_preview_filters_versions_and_sources_and_returns_stable_cursor(monkeypatch, tmp_path, capsys):
    original = tmp_path / "scan.pdf"
    original.touch()
    rows = [
        {"report_id": rid, "title": str(rid), "file_path": None,
         "analysis_json": stored, "has_body": body}
        for rid, stored, body in [(5, '{"analysis_version":99}', True),
                                (4, json.dumps({"analysis_version": str(reanalyze.ANALYSIS_VERSION)}), True),
                                (3, '{}', False), (2, '{bad', True), (1, None, False)]
    ]
    rows[-1]["file_path"] = str(original)

    def page(limit, before_id, report_id):
        return [row for row in rows if (before_id is None or row["report_id"] < before_id)
                and (report_id is None or row["report_id"] == report_id)][:limit]

    monkeypatch.setattr(reanalyze.report_analysis, "completed_attachment_page", page)
    monkeypatch.setattr(reanalyze, "upgrade", lambda rid: pytest.fail("preview invoked model"))
    assert reanalyze.main(["--limit", "1"]) == 0
    assert "--before-id 2" in capsys.readouterr().out
    assert reanalyze.candidates(5, before_id=2) == ([(1, "1", 0)], None)
    assert reanalyze.candidates(5, report_id=3) == ([], None)


def test_explicit_apply_continues_after_individual_failure(monkeypatch, capsys):
    monkeypatch.setattr(reanalyze, "candidates", lambda *args: ([(8, "甲", 1), (7, "乙", 2)], None))

    def submit(kind, report_id):
        assert kind == "report_upgrade"
        if report_id == 8:
            raise ValueError("model unavailable")

    monkeypatch.setattr(reanalyze, "enqueue", submit)
    assert reanalyze.main(["--apply"]) == 1
    output = capsys.readouterr().out
    assert "--report-id 8 --apply" in output and "#7 已排队" in output


def test_upgrade_rechecks_version_before_model_call(monkeypatch):
    from skills.base import SkillContext
    from skills.report.skill import ReportSkill

    monkeypatch.setattr(reanalyze.report_analysis, "extraction_candidates", lambda *args: [
        {"report_id": 7, "analysis_status": "done",
         "analysis_json": json.dumps({"analysis_version": reanalyze.ANALYSIS_VERSION})},
    ])
    skill = ReportSkill()
    monkeypatch.setattr(skill, "_extract_one", lambda *args: pytest.fail("newer result must be preserved"))
    context = SkillContext()
    skill._run_extract(context, {"report_id": 7, "upgrade_only": True})
    assert context.result["skipped"] == [7]
