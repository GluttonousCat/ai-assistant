"""Render the speaker and source of a report claim for downstream readers."""

from skills.report.extraction import SPEAKERS, STANCES, saved_analysis_version


def source_location(source):
    if not isinstance(source, dict) or not isinstance(source.get("quote"), str) or not source["quote"].strip():
        return ""
    number = source.get("segment")
    if type(number) is not int or number < 1:
        return ""
    page = source.get("page")
    return f"PDF 第{page}页" if type(page) is int and page > 0 else f"片段{number}"


def attributed_statement(item, statement=None):
    if not isinstance(item, dict):
        return ""
    location = source_location(item.get("source"))
    body = statement or item.get("statement")
    if not location or not isinstance(body, str) or not body.strip():
        return ""
    speaker = SPEAKERS.get(item.get("attribution"), SPEAKERS["unknown"])
    stance = STANCES.get(item.get("stance"), STANCES["unknown"])
    return f"【{speaker}·{stance}】{body}（{location}）"


def reported_rating(analysis):
    if saved_analysis_version(analysis) >= 4 and not source_location(analysis.get("rating_source")):
        return None
    return analysis.get("rating")


def core_statement(analysis):
    if saved_analysis_version(analysis) >= 4:
        evidence = analysis.get("summary_evidence")
        return attributed_statement(evidence.get("core_view")) if isinstance(evidence, dict) else ""
    return analysis.get("core_view") or analysis.get("summary") or ""


def lead_evidence(analysis):
    summary = analysis.get("summary_evidence")
    if isinstance(summary, dict):
        for item in [summary.get("core_view"), *(summary.get("key_points") or []), *(summary.get("risks") or [])]:
            if isinstance(item, dict) and source_location(item.get("source")):
                return item
    return None
