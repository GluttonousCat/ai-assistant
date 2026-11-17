"""
Research report -> industry-chain segment extraction (atomic capability)

- Input: report_id + prompt (injected by the skill layer; domain knowledge lives in beta_alpha/prompts.py)
- Output: one row per segment in fin.chain_extract (idempotent: delete then insert)
- Callers: skills/report/skill.py in a background thread after deep extraction / scripts/backfill_chain_extract.py
- Must be called outside business transactions (contains second-level LLM calls; never hold a transaction)
"""

from __future__ import annotations

from core.json_response import parse_json_response
import threading
from typing import Dict, List, Optional

from core.logger import get_logger
from harness.llm import get_extract_llm
from tools.finance import chain_extract as chain_store

logger = get_logger(__name__)

_MAX_TEXT_CHARS = 12000  # same window as report deep extraction
_DIRECTIONS = {"upstream", "midstream", "downstream", "other"}

# In-flight dedup (do not extract the same report twice)
_inflight: set = set()
_inflight_lock = threading.Lock()


def _parse_json(text: str) -> Optional[Dict]:
    if not text:
        return None
    value = text.strip()
    if value.startswith("```"):
        value = value.strip("`")
        if value.startswith("json"):
            value = value[4:]
        value = value.strip()
    return parse_json_response(value, fences="raw")


def extract_chain(report_id: int, prompt: str) -> int:
    """Extract segments from a single report and persist; returns row count
    (idempotent; call outside transactions)"""
    with _inflight_lock:
        if report_id in _inflight:
            return 0
        _inflight.add(report_id)
    try:
        return _extract_chain_inner(report_id, prompt)
    finally:
        with _inflight_lock:
            _inflight.discard(report_id)


def _extract_chain_inner(report_id: int, prompt: str) -> int:
    row = chain_store.extraction_text(report_id)
    if not row or not (row.get("content_text") or "").strip():
        return 0
    text = row["content_text"][:_MAX_TEXT_CHARS]

    try:
        raw = get_extract_llm().invoke(prompt.format(report_text=text))
        data = _parse_json(raw) or {}
    except Exception as e:
        logger.warning(f"链抽取 LLM 失败 report_id={report_id}: {e}")
        return 0

    segments = data.get("segments") or []
    rows: List[Dict] = []
    for seg in segments:
        if not isinstance(seg, dict):
            continue
        name = str(seg.get("segment") or "").strip()[:128]
        if not name:
            continue
        direction = str(seg.get("direction") or "other").strip()
        if direction not in _DIRECTIONS:
            direction = "other"
        companies = "、".join(
            str(c)[:32] for c in (seg.get("companies") or [])[:4] if c
        )
        try:
            conf = max(0.0, min(1.0, float(seg.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        rows.append(
            {
                "segment": name,
                "direction": direction,
                "companies": companies,
                "confidence": conf,
            }
        )

    chain_store.replace_segments(report_id, rows)
    logger.info(f"链抽取完成 report_id={report_id}: {len(rows)} 个环节")
    return len(rows)


def spawn_chain_extract(report_id: int) -> None:
    """Run chain extraction in a background thread (fire-and-forget hook on the
    report main pipeline; for long-lived processes only)"""

    def _worker():
        try:
            from skills.beta_alpha.prompts import CHAIN_EXTRACT_PROMPT

            extract_chain(report_id, CHAIN_EXTRACT_PROMPT)
        except Exception as e:
            logger.warning(f"后台链抽取失败 report_id={report_id}: {e}")

    t = threading.Thread(target=_worker, daemon=True, name=f"chain-extract-{report_id}")
    t.start()
