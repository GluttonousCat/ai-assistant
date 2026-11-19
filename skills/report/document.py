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
    text = row.get("content_text") or ""
    path = row.get("file_path")
    is_pdf = Path(path or row.get("file_name") or "").suffix.lower() == ".pdf"
    local_pdf = is_pdf and path and Path(path).is_file()
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
    if pages and row.get("extraction_status") != "extracted":
        return Document([], "pdf_ocr", len(pages), tuple(range(1, len(pages) + 1)))
    return Document(
        _segments([(None, text)]),
        "stored_text",
        len(pages) or None,
        incomplete_text=bool(is_pdf or (row.get("content_chars") or 0) > len(text)),
    )


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
        # Include both ends of an oversized report and spread the remaining work.
        last = len(batches) - 1
        batches = [batches[i * last // (_MAX_CHUNKS - 1)] for i in range(_MAX_CHUNKS)]
    return batches


def _terms(text: str) -> set[str]:
    terms = set(re.findall(r"[a-z0-9]+(?:\.[a-z0-9]+)*", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        terms.update(run[i:i + 2] for i in range(len(run) - 1))
    return terms


def qa_context(document: Document, question: str):
    """Select passages across the document with lexical relevance and page labels."""
    if sum(len(segment.render()) + 2 for segment in document.segments) <= _QA_CHARS:
        return document.segments
    passages = [
        Segment(number, segment.page, part)
        for number, (segment, part) in enumerate(
            ((segment, part) for segment in document.segments for part in _pieces(segment.text, 2400)), 1
        )
    ]
    query_terms = _terms(question)
    hits = [query_terms & _terms(p.text) for p in passages]
    weights = {
        term: math.log1p(len(passages) / (1 + sum(term in passage_hits for passage_hits in hits)))
        for term in query_terms
    }
    ranked = sorted(
        zip(passages, hits), key=lambda pair: (-sum(weights[t] for t in pair[1]), pair[0].number)
    )
    selected, size = [], 0
    for passage, _ in ranked:
        n = len(passage.render()) + 2
        if size + n <= _QA_CHARS:
            selected.append(passage)
            size += n
    return sorted(selected, key=lambda passage: passage.number)
