"""Extract and verify evidence per batch, then assemble one report analysis."""

from decimal import Decimal, InvalidOperation
import json
import re

from core.json_response import parse_json_response
from skills.report.document import extraction_batches
from skills.report.prompts import REPORT_EXTRACT_PROMPT, REPORT_REDUCE_PROMPT

ANALYSIS_VERSION = 2
_EVIDENCE_KINDS = {"driver", "catalyst", "valuation", "revision", "risk", "industry"}
_NUMBER = re.compile(r"\(-?\d+(?:\.\d+)?\)|[+-]?(?:\d+(?:\.\d+)?|\.\d+)")
_OVERVIEW_FIELDS = (
    "market", "ts_codes", "symbols", "company_names", "commodities", "report_type",
    "rating", "org_name", "author", "publish_date", "core_view", "key_points", "risks", "tags",
)


class InvalidExtraction(ValueError):
    """The model did not return the report object required to finish extraction."""


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
    source_text = {s.number: (s.page, _compact(s.text)) for s in segments}
    for item in list_items(items):
        if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
            continue
        source = item["source"]
        number = source.get("segment")
        quote = clean_text(source.get("quote"), 361)
        if type(number) is not int or number not in source_text or not 8 <= len(quote) <= 360:
            continue
        page, body = source_text[number]
        if _compact(quote) in body:
            yield item, {"segment": number, "page": page, "quote": quote}


def _has_value(value, quote):
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        return False
    if not number.is_finite():
        return False
    plain = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", quote.replace("−", "-"))
    for match in _NUMBER.findall(plain):
        token = "-" + match[1:-1] if match.startswith("(") and match[1] != "-" else match.strip("()")
        if number == Decimal(token):
            return True
    return False


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
    parts, forecasts, evidence = [], [], []
    seen = set()
    rejected = 0
    for index, batch in enumerate(batches, 1):
        yield {"type": "stage", "stage": "extract", "message": f"提取正文 {index}/{len(batches)} · 观点、预测与出处"}
        prompt = REPORT_EXTRACT_PROMPT.format(
            title=title, report_text="\n\n".join(segment.render() for segment in batch)
        )
        if streaming and len(batches) == 1:
            chunks = []
            for chunk in llm.stream(prompt):
                chunks.append(chunk)
                yield {"type": "delta", "text": chunk}
            raw = "".join(chunks)
        else:
            raw = llm.invoke(prompt)
        data = _parse_object(raw)

        verified_forecasts = [
            {**item, "source": source}
            for item, source in _locations(data.get("forecasts"), batch)
            if _has_value(item.get("value"), source["quote"])
        ]
        rejected += len(list_items(data.get("forecasts"))) - len(verified_forecasts)
        forecasts.extend(verified_forecasts)
        verified_notes = []
        for item, source in _locations(data.get("evidence"), batch):
            kind = clean_text(item.get("kind"), 24)
            statement = clean_text(item.get("statement"), 300)
            key = kind, _compact(statement)
            if kind in _EVIDENCE_KINDS and statement and key not in seen:
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
        evidence=evidence,
        forecasts=forecasts,
        coverage={
            "source": document.source,
            "segment_count": len(document.segments),
            "analyzed_segments": len(analyzed),
            "page_count": document.page_count,
            "missing_pages": missing,
            "partial": bool(missing or document.incomplete_text or len(analyzed) < len(document.segments)),
            "rejected_forecasts": rejected,
        },
    )
    yield {"type": "result", "data": combined}
