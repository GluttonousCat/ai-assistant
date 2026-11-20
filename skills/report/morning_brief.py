"""Prepare a source-linked morning edition from one day's newly ingested reports."""
from __future__ import annotations

from datetime import date, datetime, time, timedelta
import json
import re
from zoneinfo import ZoneInfo

from core.logger import get_logger
from skills.report.attribution import source_location
from skills.report.document import load_document
from skills.report.extraction import SPEAKERS, STANCES, locate_excerpt, saved_analysis_version, verify_claim
from tools.finance.report_analysis import reports_ingested_between

logger = get_logger(__name__)
BEIJING = ZoneInfo("Asia/Shanghai")
MORNING_CUTOFF = time(7, 30)
_ORDER = {"verified": 0, "excerpt": 1, "pending": 2, "legacy": 3, "unavailable": 4}
# An old translation sometimes adds a named speaker absent from its quote.
# When the current check cannot identify the speaker, keep just that original.
_REPORTED_SPEECH = re.compile(r"认为|称|强调|指出|表示|列示|指引|券商|分析师|本行|该行|(?:公司|管理层|受访者|该机构|报告)预计")
_ANALYST_VOICE = re.compile(
    r"^(?:we\s+(?:believe|expect|forecast|estimate|reiterate|rate|maintain|are\s+(?:buy|sell|"
    r"neutral|overweight|underweight)\b)|our\s+(?:(?:sotp|dcf|sum.of.the.parts)\s+)?"
    r"(?:valuation|price\s+(?:target|objective)|rating|estimates?))\b", re.I,
)
_QUOTED_SPEAKER = re.compile(
    r"""["“”‘’「」]|(?:^|\s)'(?:we|our)\b|\b(?:said|says|stated|told|according to|transcript|"""
    r"""Q&A|respondents?|CEO|CFO|interview(?:ee|er)?|management\s+(?:said|noted|explained|commentary|remarks))\b"""
    r"|管理层表示|问答|访谈|受访者|(?:^|\n)\s*[QA]\s*:", re.I,
)
_INTERVIEW_PAGE = re.compile(r"\b(?:transcript|interview|Q&A)\b|问答实录|访谈|(?:^|\n)\s*[QA]\s*:", re.I)
_EXPLICIT_FORECAST = re.compile(
    r"^(?:(?:we|management|the company|respondents)\s+(?:(?:do|does) not\s+)?"
    r"(?:expect|forecast|estimate|project)s?\b|(?:我们|公司|管理层|受访者)(?:预计|预期|预测))", re.I,
)
_RATING_ACTIONS = (
    ("维持|重申", r"\b(?:maintain\w*|reiterat\w*|remain\w*|retain\w*|unchanged|reaffirm\w*|keep|kept)\b"),
    ("上调|上修|调高", r"\b(?:upgrad\w*|rais\w*|lift\w*|increas\w*|revis\w* (?:up|higher))\b"),
    ("下调|下修|调低", r"\b(?:downgrad\w*|reduc\w*|lower\w*|cut|revis\w* (?:down|lower))\b"),
)


def _adds_rating_change(statement, quote):
    # “We are Neutral” gives the current rating, but says nothing about a previous rating.
    return any(re.search(r"(?:" + action + r")[^。，；]{0,14}(?:评级|目标价|估值)", statement)
               and not re.search(original + "|" + action, quote, re.I)
               for action, original in _RATING_ACTIONS)


def _legacy_speaker(source, segment, material):
    quote = source["quote"]
    if material != "original" or not _ANALYST_VOICE.match(quote):
        return "unknown"
    start = segment.text.find(quote)
    if start < 0 or _QUOTED_SPEAKER.search(quote) or _INTERVIEW_PAGE.search(segment.text):
        return "unknown"
    # A first-person sentence may continue an executive interview. Check the
    # question, speaker label and opening quotes immediately before the extract.
    before = segment.text[max(0, start - 200):start]
    after = segment.text[start + len(quote):][:80].split("\n", 1)[0]
    if _QUOTED_SPEAKER.search(before) or _QUOTED_SPEAKER.search(after) or before.rstrip().endswith((":", "：")):
        return "unknown"
    return "broker"


def _entries(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _candidates(analysis):
    """Follow the report's own overview, then look through its remaining evidence."""
    overview = analysis.get("summary_evidence")
    if isinstance(overview, dict):
        core = overview.get("core_view")
        if isinstance(core, dict):
            yield core
        yield from _entries(overview.get("key_points"))
        yield from _entries(overview.get("risks"))
    yield from _entries(analysis.get("evidence"))


def _claim(item, material, *, statement):
    source = item["source"]
    attribution = item.get("attribution")
    if not isinstance(attribution, str) or (attribution == "broker" and material != "original"):
        attribution = "unknown"
    stance = item.get("stance")
    return {
        "statement": statement, "quote": source["quote"],
        "speaker": SPEAKERS.get(attribution, SPEAKERS["unknown"]),
        "stance": STANCES.get(stance, STANCES["unknown"]) if isinstance(stance, str) else STANCES["unknown"],
        "location": source_location(source),
    }


def _report_claims(row, analysis, material):
    current = saved_analysis_version(analysis) >= 4
    candidates = [note for note in _candidates(analysis) if source_location(note.get("source"))]
    if not candidates:
        return []
    # v2/v3 lacked reliable speaker and paraphrase checks. Read the current
    # attachment once and validate each proposed quote against its own segment.
    segments = {} if current else {s.number: s for s in load_document(row).segments}
    views, risks, excerpts = [], [], []
    seen = set()
    for note in candidates:
        source = note["source"]
        key = source["segment"], source["quote"]
        if key in seen:
            continue
        seen.add(key)
        if current:
            checked = note
        else:
            segment = segments.get(source["segment"])
            if not segment:
                continue
            proposal = {**note, "attribution": _legacy_speaker(source, segment, material),
                        "stance": "expectation" if _EXPLICIT_FORECAST.match(source["quote"]) else "unknown"}
            checked = verify_claim(proposal, [segment], material=material)
            if (checked and checked["attribution"] == "unknown" and checked["statement"] != checked["source"]["quote"]
                    and (_REPORTED_SPEECH.search(checked["statement"])
                         or (row.get("org_name") and row["org_name"] in checked["statement"]))):
                checked = None
            if not checked:
                original = locate_excerpt(note, [segment])
                if original:
                    excerpts.append(_claim({"source": original}, material, statement=""))
                continue
        statement = checked.get("statement")
        if not isinstance(statement, str) or not statement.strip():
            continue
        if statement != checked["source"]["quote"] and _adds_rating_change(statement, checked["source"]["quote"]):
            excerpts.append(_claim({"source": checked["source"]}, material, statement=""))
            continue
        group = risks if note.get("kind") == "risk" or checked.get("stance") == "risk" else views
        group.append(_claim(checked, material, statement=statement))
    views.sort(key=lambda c: c["speaker"] != SPEAKERS["broker"])
    if views and risks:
        return [views[0], risks[0]]
    return (views or risks or excerpts)[:2]


def _edition_report(row):
    analysis = row.get("analysis_json")
    if isinstance(analysis, str):
        try:
            analysis = json.loads(analysis)
        except ValueError:
            analysis = {}
    if not isinstance(analysis, dict):
        analysis = {}
    material = analysis.get("material_type")
    if material not in ("original", "commentary"):
        material = "unknown"
    claims = []
    version = saved_analysis_version(analysis)
    processing = row.get("analysis_status")
    extraction = row.get("extraction_status")
    if processing == "done":
        try:
            claims = _report_claims(row, analysis, material)
        except OSError as exc:
            logger.warning("早报核对历史原件失败 report_id=%s: %s", row["report_id"], type(exc).__name__)
    if claims:
        status = "verified" if claims[0]["statement"] else "excerpt"
        reason = ""
    elif processing == "failed" or extraction == "failed":
        status, reason = "unavailable", "解析失败"
    elif extraction == "pending_ocr":
        status, reason = "pending", "待识别扫描件"
    elif processing != "done":
        status, reason = "pending", "待完成解析"
    elif version < 4:
        status, reason = "legacy", "旧版分析缺少可核对的引文"
    else:
        status, reason = "unavailable", "暂无可核对的观点"
    coverage = analysis.get("coverage")
    return {
        "report_id": row["report_id"], "title": row.get("title") or row.get("file_name") or "未命名研报",
        "org": row.get("org_name") or "", "target": row.get("target") or row.get("ts_code") or "",
        "market": row.get("market") or "", "material_type": material, "claims": claims,
        "status": status, "pending_reason": reason, "analysis_version": version,
        "partial": isinstance(coverage, dict) and coverage.get("partial") is True,
    }


def load_morning_brief(issue_date: date):
    return load_brief_ending_at(datetime.combine(issue_date, MORNING_CUTOFF, BEIJING))


def load_brief_ending_at(end: datetime):
    """Read the 24 hours ending at an explicit, timezone-aware cutoff."""
    if end.utcoffset() is None:
        raise ValueError("研报截止时间必须带时区")
    end = end.astimezone(BEIJING)
    start = end - timedelta(days=1)
    rows = reports_ingested_between(start, end)
    reports = [_edition_report(row) for row in rows]
    # Within each tier the original ingestion order (newest first) is stable.
    reports.sort(key=lambda r: (_ORDER[r["status"]],
                                not any(c["speaker"] == SPEAKERS["broker"] for c in r["claims"]),
                                r["material_type"] == "commentary", r["material_type"] != "original"))
    return {"issue_date": end.date(), "window_start": start, "window_end": end,
            "total_count": len(rows), "reports": reports}
