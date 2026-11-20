"""Page-aware report inputs for extraction and single-report Q&A."""

from dataclasses import dataclass
import math
from pathlib import Path
import re

from tools.finance.report_extractor import get_extractor

_OCR_PAGE = re.compile(r"^\[\[PDF_PAGE:([1-9]\d*)\]\][ \t]*\r?\n", re.MULTILINE)
_CHUNK_CHARS = 24_000
_MAX_CHUNKS = 12
_SEGMENT_CHARS = 8_000
_QA_CHARS = 12_000
# Prefer informative sections within each band of an oversized report.
# Match section names and measure labels rather than isolated market keywords.
_RESEARCH_SECTIONS = tuple(re.compile(pattern, re.I) for pattern in (
    r"投资逻辑|核心假设|核心观点|investment thesis|key assumptions?|investment case",
    r"盈利预测|估值方法|目标价|forecast revisions?|earnings estimates?|price target|valuation method",
    r"超预期|预期差|一致预期|较前次|vs\.? consensus|versus consensus|estimate changes?",
    r"催化剂|催化因素|敏感性分析|风险因素|sensitivity analysis|key risks?|catalysts?",
))


def _section_score(batch):
    body = "\n".join(segment.text for segment in batch)
    return sum(bool(pattern.search(body)) for pattern in _RESEARCH_SECTIONS)


@dataclass(frozen=True)
class Segment:
    number: int
    page: int | None
    text: str

    def render(self) -> str:
        page = f" | PDF 第 {self.page} 页" if self.page is not None else ""
        return f"[片段 {self.number}{page}]\n{self.text}"


@dataclass(frozen=True)
class Document:
    segments: list[Segment]
    source: str
    page_count: int | None = None
    missing_pages: tuple[int, ...] = ()
    incomplete_text: bool = False


def _pieces(text: str, limit: int):
    while len(text) > limit:
        end = text.rfind("\n", limit // 2, limit)
        if end == -1:
            end = limit
        yield text[:end]
        text = text[end:].lstrip()
    if text:
        yield text


def _segments(pages):
    segments = []
    for page, text in pages:
        for part in _pieces(text.strip(), _SEGMENT_CHARS):
            segments.append(Segment(len(segments) + 1, page, part))
    return segments


def load_document(row) -> Document:
    """Prefer the original PDF layout when available, retaining legacy text fallbacks."""
    # Older pending attachments may carry the posting author's commentary in
    # content_text. Only the recovered attachment can establish their source.
    path = row.get("file_path")
    zsxq_attachment = row.get("source") in ("zsxq", "zsxq_file")
    text = row.get("content_text") or ""
    if (zsxq_attachment and row.get("extraction_status") in ("pending", "pending_ocr")
            and not _OCR_PAGE.match(text)):
        text = ""
    is_pdf = Path(path or row.get("file_name") or "").suffix.lower() == ".pdf"
    local_file = bool(path and Path(path).is_file())
    local_pdf = is_pdf and local_file
    markers = list(_OCR_PAGE.finditer(text))
    if markers and markers[0].start() == 0:
        ends = [marker.start() for marker in markers[1:]] + [len(text)]
        pages = [(int(marker[1]), text[marker.end():end]) for marker, end in zip(markers, ends)]
        total = len(get_extractor().pdf_pages(path, layout=False)) if local_pdf else None
        total = total or None
        seen = {page for page, body in pages if body.strip()}
        missing = tuple(page for page in range(1, (total or 0) + 1) if page not in seen)
        return Document(_segments(pages), "pdf_ocr", total, missing, total is None)

    pages = get_extractor().pdf_pages(path) if local_pdf else []
    if pages and any(page.strip() for page in pages):
        missing = tuple(i for i, body in enumerate(pages, 1) if not body.strip())
        return Document(_segments(enumerate(pages, 1)), "pdf_text", len(pages), missing)
    if pages and (zsxq_attachment or row.get("extraction_status") != "extracted"):
        return Document([], "pdf_ocr", len(pages), tuple(range(1, len(pages) + 1)))
    if zsxq_attachment and (not local_file or local_pdf):
        # Older ingestion also merged posting text into "extracted" attachment
        # rows. Without a file path or usable local PDF pages its origin is ambiguous.
        return Document([], "stored_text", incomplete_text=True)
    suffix = Path(path or "").suffix.lower()
    if suffix in {".docx", ".doc", ".txt", ".md", ".markdown"} and (
        not text or (row.get("content_chars") or 0) > len(text)
    ) and Path(path).is_file():
        complete = get_extractor().extract(path)
        if complete and len(complete) > len(text):
            return Document(_segments([(None, complete)]), "local_file")
    return Document(
        _segments([(None, text)]),
        "stored_text",
        len(pages) or None,
        incomplete_text=bool(is_pdf or (row.get("content_chars") or 0) > len(text)),
    )


def reading_status(row, document: Document) -> dict:
    pages = len({s.page for s in document.segments if s.page is not None})
    if document.segments:
        partial = document.incomplete_text or bool(document.missing_pages)
        status = "partial" if partial else "available"
        note = "部分正文可读，结论限于已读取的内容。" if partial else "正文可读。"
    elif row.get("extraction_status") == "pending_ocr":
        status, note = "pending_ocr", "附件正在等待 OCR 结果。"
    elif document.source == "pdf_ocr" and document.page_count:
        if row.get("extraction_status") == "extracted" and (row.get("content_text") or "").strip():
            status, note = "legacy_unverified", "扫描件只有旧版文字，出处和页码待核对，请勿据此引用券商观点。"
        else:
            status, note = "needs_ocr", "已找到 PDF，尚无可定位的正文，可在研报详情重新分析以识别扫描页。"
    elif row.get("source") in ("zsxq", "zsxq_file") and not Path(row.get("file_path") or "").is_file():
        status, note = "awaiting_attachment", "本地缺少对应附件，尚无可定位的正文。"
    else:
        status, note = "unavailable", "当前记录没有可核对的正文。"
    return {"body_status": status, "body_note": note, "pages_with_text": pages if document.page_count or pages else None,
            "page_count": document.page_count}


def extraction_batches(document: Document):
    batches = []
    batch, size = [], 0
    for segment in document.segments:
        n = len(segment.render()) + 2
        if batch and size + n > _CHUNK_CHARS:
            batches.append(batch)
            batch, size = [], 0
        batch.append(segment)
        size += n
    if batch:
        batches.append(batch)
    if len(batches) > _MAX_CHUNKS:
        # Preserve both ends and coverage across the source. Within each band
        # prioritize sections that explain revisions, assumptions or catalysts.
        last = len(batches) - 1
        bands = _MAX_CHUNKS - 2
        selected = [batches[0]]
        for i in range(bands):
            start = 1 + i * (last - 1) // bands
            stop = 1 + (i + 1) * (last - 1) // bands
            middle = (start + stop - 1) / 2
            best = max(range(start, stop), key=lambda j: (_section_score(batches[j]), -abs(j - middle)))
            selected.append(batches[best])
        batches = [*selected, batches[-1]]
    return batches


def _terms(text: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def _source_hints(analysis, query_terms):
    """Use indexed Chinese statements to locate their quotes in the current original."""
    hints = {}
    if not isinstance(analysis, dict):
        return hints
    groups = [analysis.get("evidence"), analysis.get("forecasts")]
    insights = analysis.get("research_insights")
    if isinstance(insights, dict):
        groups.extend(insights.values())
    for group in groups:
        if not isinstance(group, list):
            continue
        for item in group:
            if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
                continue
            source = item["source"]
            quote = source.get("quote")
            number = source.get("segment")
            if type(number) is not int or not isinstance(quote, str) or not 8 <= len(quote) <= 360:
                continue
            labels = " ".join(
                item[k] for k in ("statement", "subject", "indicator", "condition", "horizon", "raw_text")
                if isinstance(item.get(k), str)
            )
            matched = query_terms & _terms(labels)
            if matched:
                hints.setdefault(number, []).append(("".join(quote.casefold().split()), matched))
    return hints


def qa_context(document: Document, question: str, analysis=None, *, max_chars=_QA_CHARS):
    """Rank original passages, including cross-language matches through checked quotes."""
    if sum(len(segment.render()) + 2 for segment in document.segments) <= max_chars:
        return document.segments
    query_terms = _terms(question)
    hints = _source_hints(analysis, query_terms)
    passages, hits = [], []
    for segment in document.segments:
        for part in _pieces(segment.text, 2400):
            passages.append(Segment(segment.number, segment.page, part))
            terms = query_terms & _terms(part)
            if segment.number in hints:
                body = "".join(part.casefold().split())
                for quote, translated_terms in hints[segment.number]:
                    if quote in body:
                        terms.update(translated_terms)
            hits.append(terms)
    weights = {
        term: math.log1p(len(passages) / (1 + sum(term in passage_hits for passage_hits in hits)))
        for term in query_terms
    }
    ranked = sorted(
        range(len(passages)), key=lambda i: -sum(weights[t] for t in sorted(hits[i]))
    )
    selected, size = [], 0
    for i in ranked:
        n = len(passages[i].render()) + 2
        if size + n <= max_chars:
            selected.append(i)
            size += n
    return [passages[i] for i in sorted(selected)]
