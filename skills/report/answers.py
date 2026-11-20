"""Render report answers only from claims linked to the passages supplied to the model."""

import json

from core.json_response import parse_json_response
from skills.report.attribution import SPEAKERS, STANCES, source_location
from skills.report.document import Segment, _terms, qa_context
from skills.report.extraction import locate_excerpt, verify_claim
from skills.report.prompts import REPORT_QA_PROMPT, REPORT_SUMMARY_PROMPT


def cited_passages(analysis, question, document, limit=10):
    """Use saved claims as search hints and restore nearby text from the current source."""
    items = []
    overview = analysis.get("summary_evidence")
    if isinstance(overview, dict):
        items.append(overview.get("core_view"))
    groups = analysis.get("research_insights")
    if isinstance(groups, dict):
        for entries in groups.values():
            if isinstance(entries, list):
                items.extend(entries)
    for key in ("evidence", "forecasts"):
        if isinstance(analysis.get(key), list):
            items.extend(analysis[key])
    if isinstance(analysis.get("forecast_excerpts"), list):
        items.extend({"source": quote} for quote in analysis["forecast_excerpts"] if isinstance(quote, dict))
    query = _terms(question)
    candidates = {}
    current = {part.number: part for part in document.segments}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
            continue
        source = item["source"]
        number, page, quote = source.get("segment"), source.get("page"), source.get("quote")
        if (type(number) is not int or number < 1 or not isinstance(quote, str) or not 8 <= len(quote.strip()) <= 360
                or (page is not None and (type(page) is not int or page < 1))):
            continue
        terms = _terms(" ".join(str(item.get(k) or "") for k in ("statement", "indicator", "raw_text", "metric")))
        passage = Segment(number, page, quote)
        if current:
            part = current.get(number)
            if part is None or part.page != page:
                continue
            start = part.text.find(quote)
            if start < 0:
                continue
            passage = Segment(number, page, part.text[max(0, start - 200):start + len(quote) + 200])
        score = len(query & (terms | _terms(quote)))
        candidates[passage] = max(candidates.get(passage, 0), score)
    return sorted(candidates, key=candidates.get, reverse=True)[:limit]


def comparison_passages(analysis, question, document):
    if not document.segments:
        return cited_passages(analysis, question, document)
    # An earlier extraction may have focused on a different question. Reserve
    # room for a fresh search even when saved claims have valid citations.
    passages = cited_passages(analysis, question, document, limit=3)
    budget = 6000 - sum(len(p.render()) + 2 for p in passages)
    for part in qa_context(document, question, analysis, max_chars=budget):
        passages = [p for p in passages if not (p.number == part.number and p.text in part.text)]
        passages.append(part)
    return passages


def indexed_original_quote(analysis, document):
    """Recover a concise original quotation for the selected report's own topic."""
    if analysis.get("material_type") != "original":
        return None
    summary = analysis.get("summary_evidence") or {}
    notes = [summary.get("core_view"), *(summary.get("key_points") or [])] if isinstance(summary, dict) else []
    forecasts = analysis.get("forecast_excerpts")
    if isinstance(forecasts, list):
        notes.extend({"source": src} for src in forecasts)
    if isinstance(analysis.get("evidence"), list):
        notes.extend(analysis["evidence"])
    pages = {s.number: s for s in document.segments}
    for note in notes:
        source = note.get("source") if isinstance(note, dict) else None
        if not isinstance(source, dict) or type(source.get("segment")) is not int:
            continue
        part = pages.get(source["segment"])
        if part and part.page == source.get("page"):
            located = locate_excerpt(note, [part])
            if located:
                return located
    return None


def answer_from_sources(llm, materials, question, *, compare=False):
    """The model supplies candidate claims. The server supplies location, identity and quotes."""
    by_id = {meta["report_id"]: (meta, passages) for meta, passages in materials if passages}
    citations, excerpts = [], []
    if by_id:
        prompt = REPORT_SUMMARY_PROMPT if compare else REPORT_QA_PROMPT
        if any(meta.get("topics") for meta, _ in by_id.values()):
            prompt += "\n各条 claim 另填 topic，取该篇 topics 中原句直接回答的一项。分别覆盖有依据的主题。"
        documents = [{**meta, "passages": [p.render() for p in passages]}
                     for meta, passages in by_id.values()]
        raw = llm.invoke(prompt.format(materials=json.dumps(documents, ensure_ascii=False), question=question))
        result = parse_json_response(raw)
        claims = result.get("claims") if isinstance(result, dict) else None
        seen = set()
        for item in claims[:24] if isinstance(claims, list) else ():
            if not isinstance(item, dict):
                continue
            rid = item.get("report_id")
            if type(rid) is not int or rid not in by_id:
                continue
            meta, passages = by_id[rid]
            if topics := meta.get("topics"):
                topic = item.get("topic", topics[0] if len(topics) == 1 else None)
                if topic not in topics:
                    continue
                meta = {**meta, "topic": topic}
            claim = verify_claim(item, passages, material=meta["material_type"])
            if not claim:
                # A failed paraphrase may still point to an authentic passage.
                # Preserve that passage for inspection without its proposed interpretation.
                source = locate_excerpt(item, passages)
                if source and (rid, meta.get("topic"), source["quote"]) not in seen:
                    excerpts.append({**meta, "source": source, "statement": "",
                                     "attribution": "unknown", "stance": "unknown"})
                    seen.add((rid, meta.get("topic"), source["quote"]))
                continue
            key = (rid, claim["statement"], claim["source"]["quote"])
            if key not in seen:
                citations.append({**meta, **claim})
                seen.add(key)

    if not citations and not excerpts:
        return {"summary": "所查材料中没有找到可核对、足以回答这个问题的原文。", "citations": []}
    if any(meta.get("topics") for meta, _ in by_id.values()):
        # A checked statement in another report cannot replace this original's
        # locatable quote. Keep one excerpt where the paraphrase failed checks.
        covered = {(c["topic"], c["report_id"]) for c in citations}
        for excerpt in excerpts:
            key = excerpt["topic"], excerpt["report_id"]
            if key not in covered:
                citations.append(excerpt)
                covered.add(key)
    elif not citations:
        citations = excerpts[:3]
    return {"summary": render_citations(citations), "citations": citations}


def render_citations(citations):
    """Render already checked claims without a second model paraphrase."""
    lines = []
    if citations and not any(c["statement"] for c in citations):
        lines.append("以下是可定位的原文，中文解读尚待核实。")
    for claim in citations:
        source = claim["source"]
        where = source_location(source)
        identity = claim.get("org") or claim["title"] or "研报"
        if claim["material_type"] == "commentary":
            identity = f"二手材料转述，涉及{identity}"
        elif claim["material_type"] == "unknown":
            identity = f"{identity}，材料性质待核实"
        label = f"{SPEAKERS[claim['attribution']]}·{STANCES[claim['stance']]}" if claim["statement"] else "原文"
        date = f"，入库资料发布日期 {claim['publish_date']}" if claim.get("publish_date") else ""
        heading = f"{label}（{identity}{date}，研报 #{claim['report_id']}，{where}）"
        if claim.get("source_basis") == "saved_excerpt":
            heading += " · 分析时保存的摘录"
        translation = claim["statement"] if claim["statement"] != source["quote"] else ""
        lines.append("\n\n".join(filter(None, (
            heading, translation, "> " + source["quote"].replace("\n", "\n> "),
        ))))
    return "\n\n".join(lines)
