"""
研报 -> 产业链环节抽取 (原子能力)

- 输入: report_id + prompt (由 skill 层注入, 专业知识放 beta_alpha/prompts.py)
- 输出: fin.chain_extract 每环节一行 (幂等: 先删后插)
- 调用方: skills/report/skill.py 深度提取后后台线程 / scripts/backfill_chain_extract.py
- 必须在业务事务外调用 (含 LLM 秒级调用, 禁止占事务)
"""
from __future__ import annotations

import json
import threading
from typing import Dict, List, Optional

from core.logger import get_logger
from core.llm.client import get_extract_llm
from storage.pg import PgClient
from skills.beta_alpha.schema import init_chain_extract_schema

logger = get_logger(__name__)

_MAX_TEXT_CHARS = 12000       # 与研报深度提取同窗口
_DIRECTIONS = {"upstream", "midstream", "downstream", "other"}

# 在途去重 (同一报告不重复抽取)
_inflight: set = set()
_inflight_lock = threading.Lock()


def _parse_json(text: str) -> Optional[Dict]:
    if not text:
        return None
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        import re
        m = re.search(r"\{.*\}", t, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except json.JSONDecodeError:
                return None
    return None


def extract_chain(report_id: int, prompt: str) -> int:
    """单篇研报抽取环节并入库, 返回入库行数 (幂等, 事务外调用)"""
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
    with PgClient() as pg:
        init_chain_extract_schema(pg)
        row = pg.fetch_one(
            "SELECT content_text FROM fin.report_meta WHERE report_id=%s",
            (report_id,))
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
            str(c)[:32] for c in (seg.get("companies") or [])[:4] if c)
        try:
            conf = max(0.0, min(1.0, float(seg.get("confidence", 0.5))))
        except (TypeError, ValueError):
            conf = 0.5
        rows.append({"segment": name, "direction": direction,
                     "companies": companies, "confidence": conf})

    with PgClient() as pg:
        pg.execute("DELETE FROM fin.chain_extract WHERE report_id=%s", (report_id,))
        if rows:
            pg.executemany(
                "INSERT INTO fin.chain_extract (report_id, segment, direction, "
                "companies, confidence) VALUES (%s, %s, %s, %s, %s)",
                [(report_id, r["segment"], r["direction"], r["companies"],
                  r["confidence"]) for r in rows])
    logger.info(f"链抽取完成 report_id={report_id}: {len(rows)} 个环节")
    return len(rows)


def spawn_chain_extract(report_id: int) -> None:
    """后台线程执行链抽取 (fire-and-forget, 研报主链路挂点; 仅限常驻进程使用)"""
    def _worker():
        try:
            from skills.beta_alpha.prompts import CHAIN_EXTRACT_PROMPT
            extract_chain(report_id, CHAIN_EXTRACT_PROMPT)
        except Exception as e:
            logger.warning(f"后台链抽取失败 report_id={report_id}: {e}")

    t = threading.Thread(target=_worker, daemon=True,
                         name=f"chain-extract-{report_id}")
    t.start()
