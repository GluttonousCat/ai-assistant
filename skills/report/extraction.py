"""Extract and verify evidence per batch, then assemble one report analysis."""

from decimal import Decimal, InvalidOperation
import json
import re

from core.json_response import parse_json_response
from core.logger import get_logger
from skills.report.document import extraction_batches
from skills.report.prompts import REPORT_EXTRACT_PROMPT, REPORT_LOCATE_PROMPT, REPORT_REDUCE_PROMPT

logger = get_logger(__name__)

ANALYSIS_VERSION = 3
_EVIDENCE_KINDS = {"driver", "catalyst", "valuation", "revision", "risk", "industry"}
_INSIGHT_TYPES = ("theses", "assumptions", "changes", "watchpoints")
_NUMBER = re.compile(r"\(-?\d+(?:\.\d+)?\)|[+-]?(?:\d+(?:\.\d+)?|\.\d+)")
_OVERVIEW_FIELDS = (
    "market", "ts_codes", "symbols", "company_names", "commodities", "report_type",
    "rating", "org_name", "author", "publish_date", "core_view", "key_points", "risks", "tags",
)


class InvalidExtraction(ValueError):
    """The model did not return the report object required to finish extraction."""


def saved_analysis_version(raw):
    """Unversioned or malformed historical output predates versioned extraction."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return 0
    version = raw.get("analysis_version") if isinstance(raw, dict) else None
    if type(version) is int:
        return version
    if isinstance(version, str) and version.isascii() and version.isdigit() and len(version) <= 9:
        return int(version)
    return 0


def clean_text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def list_items(value, limit=100):
    return value[:limit] if isinstance(value, list) else []


def text_items(value, limit, chars):
    return list(dict.fromkeys(
        text for item in list_items(value) if (text := clean_text(item, chars))
    ))[:limit]


def _compact(text):
    return re.sub(r"\s+", "", text).casefold()


def _locations(items, segments):
    source_text = {s.number: s for s in segments}
    for item in list_items(items):
        if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
            continue
        source = item["source"]
        number = source.get("segment")
        quote = clean_text(source.get("quote"), 361)
        if type(number) is not int or number not in source_text or not 8 <= len(quote) <= 360:
            continue
        segment = source_text[number]
        if not any(c.isalnum() for c in quote):
            continue
        if quote not in segment.text:
            # The model may flatten line breaks in tables. Store the actual matched
            # text so the citation still reproduces the page's case and spacing.
            pattern = r"\s*".join(re.escape(char) for char in quote if not char.isspace())
            match = re.search(pattern, segment.text, flags=re.IGNORECASE)
            if not match:
                continue
            quote = match.group()
        yield item, {"segment": number, "page": segment.page, "quote": quote}


def _numbers(text):
    plain = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", text.replace("−", "-"))
    for match in _NUMBER.findall(plain):
        token = "-" + match[1:-1] if match.startswith("(") and match[1] != "-" else match.strip("()")
        yield Decimal(token)


def _has_value(value, quote):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    return number.is_finite() and number in _numbers(quote)


def _verified_insight(item, statement, source, quoted_numbers):
    fields = {
        key: value
        for key in ("subject", "indicator", "condition", "horizon")
        if (value := clean_text(item.get(key), 120))
    }
    if item.get("insight_type") == "watchpoints" and not (fields.get("indicator") or fields.get("condition")):
        return None
    # A real quote alone does not support an invented price threshold or date.
    if any(n not in quoted_numbers for value in fields.values() for n in _numbers(value)):
        return None
    return {"statement": statement, **fields, "source": source}


def _note_sources(claims, proposals, batch):
    """Bind only known claim IDs; replacement model output may only supply a citation."""
    sources = {}
    for item, source in _locations(proposals, batch):
        index = item.get("id")
        if type(index) is int and index in claims:
            statement = claims[index]["statement"]
            if set(_numbers(statement)).issubset(_numbers(source["quote"])):
                sources.setdefault(index, source)
    return sources


def _relocate_notes(llm, claims, batch, body):
    payload = [
        {"id": index, **{key: clean_text(item.get(key), 300) for key in
                         ("statement", "subject", "condition", "indicator", "horizon") if item.get(key)}}
        for index, item in claims.items()
    ]
    try:
        raw = llm.invoke(REPORT_LOCATE_PROMPT.format(claims=json.dumps(payload, ensure_ascii=False), report_text=body))
        data = parse_json_response(raw)
        return _note_sources(claims, data.get("sources"), batch) if isinstance(data, dict) else {}
    except Exception as exc:
        logger.warning("研报出处复核调用失败，保留已核对结果: %s", type(exc).__name__)
        return {}


def _overview(data):
    out = {key: clean_text(data.get(key), 200) for key in _OVERVIEW_FIELDS}
    for key in ("ts_codes", "symbols", "company_names", "commodities", "key_points", "risks"):
        out[key] = text_items(data.get(key), 10, 150)
    tags = data.get("tags")
    out["tags"] = {
        key: text_items(tags.get(key), 12, 64)
        for key in ("targets", "industries", "regions", "commodities", "themes")
    } if isinstance(tags, dict) else {}
    return out


def _parse_object(raw):
    data = parse_json_response(raw)
    if not isinstance(data, dict) or not any(key in data for key in ("core_view", "key_points", "market", "evidence", "forecasts")):
        raise InvalidExtraction("模型输出缺少有效的研报分析 JSON")
    return data


def analyze_document(document, title, llm, *, streaming=False):
    """Yield progress, optional model deltas, and one assembled result."""
    batches = extraction_batches(document)
    if not batches:
        raise InvalidExtraction("研报没有可分析的正文")
    # The feed's short, signed commentary covers differ from the distribution
    # watermark on broker originals. Match only an actual first page.
    first = document.segments[0]
    signed_cover = first.page == 1 and len(first.text) < 500 and bool(
        re.search(r"(?:^|\n)公众号[：:]\s*猫哥读研报\s*$", first.text)
    )
    material = "commentary" if signed_cover else "unknown"
    material_source = ({"segment": first.number, "page": 1, "quote": first.text.strip()[-360:]}
                       if signed_cover else None)
    parts, forecasts, evidence = [], [], []
    insights = {name: [] for name in _INSIGHT_TYPES}
    seen = set()
    seen_insights = set()
    rejected = rejected_evidence = rejected_insights = 0
    repair_attempts = recovered = 0
    for index, batch in enumerate(batches, 1):
        yield {"type": "stage", "stage": "extract", "message": f"提取正文 {index}/{len(batches)} · 观点、预测与出处"}
        body = "\n\n".join(segment.render() for segment in batch)
        prompt = REPORT_EXTRACT_PROMPT.format(title=title, report_text=body)
        if streaming and len(batches) == 1:
            chunks = []
            for chunk in llm.stream(prompt):
                chunks.append(chunk)
                yield {"type": "delta", "text": chunk}
            raw = "".join(chunks)
        else:
            raw = llm.invoke(prompt)
        data = _parse_object(raw)
        if index == 1 and material == "unknown":
            for item, source in _locations([data.get("material")], batch):
                kind = item.get("type")
                if kind in ("original", "commentary") and source["segment"] == first.number and first.page in (None, 1):
                    material, material_source = kind, source

        verified_forecasts = [
            {**item, "source": source}
            for item, source in _locations(data.get("forecasts"), batch)
            if _has_value(item.get("value"), source["quote"])
        ]
        rejected += len(list_items(data.get("forecasts"))) - len(verified_forecasts)
        forecasts.extend(verified_forecasts)
        verified_notes = []
        notes = list_items(data.get("evidence"))
        claims = {i: item for i, item in enumerate(notes, 1) if isinstance(item, dict)
                  and clean_text(item.get("kind"), 24) in _EVIDENCE_KINDS and clean_text(item.get("statement"), 300)}
        sources = _note_sources(claims, [{"id": i, "source": item.get("source")} for i, item in claims.items()], batch)
        if not repair_attempts and len(claims) >= 4 and 2 * len(sources) < len(claims):
            repair_attempts += 1
            yield {"type": "stage", "stage": "parse", "message": "复核未定位的证据出处"}
            missing = {i: item for i, item in claims.items() if i not in sources}
            repaired = _relocate_notes(llm, dict(list(missing.items())[:10]), batch, body)
            recovered += len(repaired)
            sources.update(repaired)
        rejected_evidence += len(notes) - len(sources)
        for i, item in claims.items():
            if i not in sources:
                continue
            source = sources[i]
            kind = clean_text(item.get("kind"), 24)
            statement = clean_text(item.get("statement"), 300)
            key = kind, _compact(statement)
            quoted_numbers = set(_numbers(source["quote"]))
            role = clean_text(item.get("insight_type"), 24)
            if role in insights:
                insight = _verified_insight(item, statement, source, quoted_numbers)
                if insight is not None:
                    insight_key = (role, tuple((k, _compact(v)) for k, v in insight.items() if k != "source"))
                    if insight_key not in seen_insights:
                        seen_insights.add(insight_key)
                        insights[role].append(insight)
                else:
                    rejected_insights += 1
            if key in seen:
                continue
            seen.add(key)
            verified_notes.append({"kind": kind, "statement": statement, "source": source})
        evidence.extend(verified_notes)
        parts.append({
            **_overview(data),
            "verified_observations": [
                note["statement"] for note in verified_notes[:8]
            ],
        })

    if len(parts) == 1:
        combined = {key: parts[0][key] for key in _OVERVIEW_FIELDS}
    else:
        yield {"type": "stage", "stage": "synthesize", "message": "合并各段观点与风险"}
        prompt = REPORT_REDUCE_PROMPT.format(title=title, parts=json.dumps(parts, ensure_ascii=False))
        if streaming:
            chunks = []
            for chunk in llm.stream(prompt):
                chunks.append(chunk)
                yield {"type": "delta", "text": chunk}
            raw = "".join(chunks)
        else:
            raw = llm.invoke(prompt)
        combined = _overview(_parse_object(raw))

    analyzed = [segment for batch in batches for segment in batch]
    missing = sorted(set(document.missing_pages) | (
        {segment.page for segment in document.segments if segment.page is not None}
        - {segment.page for segment in analyzed}
    ))
    combined.update(
        analysis_version=ANALYSIS_VERSION,
        material_type=material,
        material_source=material_source,
        evidence=evidence,
        research_insights=insights,
        forecasts=forecasts,
        coverage={
            "source": document.source,
            "segment_count": len(document.segments),
            "analyzed_segments": len(analyzed),
            "page_count": document.page_count,
            "missing_pages": missing,
            "partial": bool(missing or document.incomplete_text or len(analyzed) < len(document.segments)),
            "rejected_forecasts": rejected,
            "rejected_evidence": rejected_evidence,
            "rejected_insights": rejected_insights,
            "evidence_repair_attempts": repair_attempts,
            "recovered_evidence": recovered,
        },
    )
    yield {"type": "result", "data": combined}
