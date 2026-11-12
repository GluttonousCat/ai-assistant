# -*- encoding: utf-8 -*-
"""
Report body Chinese translation (English original → report_meta.content_text_cn)

- Ingestion path: after deep extraction, spawn_translate() starts a background
  thread (same pattern as chain extraction; failure does not affect the main flow);
- Translation scope: the first _MAX_SRC_CHARS characters of the body, aligned with
  the deep-extraction window (the frontend detail page only shows the first 8000
  chars; translating the tail of an overlong original adds no reading value and
  controls cost);
- Chinese-dominant bodies are not translated (content_text_cn stays empty, the
  read side falls back to the original);
- Backfill for existing rows: python -m skills.report.translate [--limit N] [--workers 3]
"""
from __future__ import annotations

import re
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import List, Optional

from core.logger import get_logger
from storage.pg import PgClient
from skills.report.prompts import TRANSLATE_PROMPT

logger = get_logger(__name__)

# Translation window aligned with skill._MAX_TEXT_CHARS (source characters)
_MAX_SRC_CHARS = 12000
# Per-chunk limit sent for translation (English chars → Chinese output tokens is
# roughly 1:1.2; leave budget for thinking)
_CHUNK_CHARS = 6000

# Translation concurrency throttle: at most 3 parallel calls to the LLM gateway
_TRANSLATE_SEMAPHORE = threading.BoundedSemaphore(3)

_CJK = re.compile(r"[\u4e00-\u9fff]")


def is_en_text(s: Optional[str], min_len: int = 20) -> bool:
    """Treated as English-dominant when the CJK character ratio < 0.3 (same
    criterion as the metadata repair)"""
    s = str(s or "").strip()
    if len(s) < min_len:
        return False
    return len(_CJK.findall(s)) / len(s) < 0.3


def _split_chunks(text: str, size: int = _CHUNK_CHARS) -> List[str]:
    """Split on paragraph boundaries, each chunk ≤ size chars; a single
    overlong line (linearized table) is hard-cut"""
    chunks: List[str] = []
    buf: List[str] = []
    n = 0
    for line in text.splitlines(keepends=True):
        if n + len(line) > size and buf:
            chunks.append("".join(buf))
            buf, n = [], 0
        while len(line) > size:
            chunks.append(line[:size])
            line = line[size:]
        buf.append(line)
        n += len(line)
    if buf:
        chunks.append("".join(buf))
    return chunks


def translate_body(text: str) -> str:
    """Translate the (already windowed by the caller) body chunk by chunk in
    sequence, join and return; escalating-timeout retry per chunk"""
    from harness.llm import get_extract_llm
    out: List[str] = []
    for chunk in _split_chunks(text):
        resp = ""
        # long chunks may exceed 180s in thinking mode: retry with escalating
        # timeouts; only re-raise after all 3 attempts fail
        for attempt, timeout in enumerate((180.0, 240.0, 300.0), 1):
            try:
                resp = (get_extract_llm(timeout=timeout).invoke(
                    TRANSLATE_PROMPT.format(text=chunk)) or "").strip()
                break
            except Exception as e:
                if attempt == 3:
                    raise
                logger.warning(f"翻译单片超时/失败, 重试 ({attempt}/3): {str(e)[:80]}")
        if resp.startswith("```"):  # leniently strip the code fence
            resp = resp.strip("`").strip()
            if resp.startswith("text"):
                resp = resp[4:]
        out.append(resp)
    return "\n".join(out)


def translate_report(report_id: int) -> bool:
    """Translate one report: English-dominant body without a translation ->
    content_text_cn; returns whether a row was written"""
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT report_id, content_text, content_text_cn FROM fin.report_meta "
            "WHERE report_id=%s", (report_id,))
    if not row or row.get("content_text_cn"):
        return False
    text = (row.get("content_text") or "")[:_MAX_SRC_CHARS]
    if len(text.strip()) < 200 or not is_en_text(text):
        return False  # Chinese body / too short / already translated: idempotent skip
    with _TRANSLATE_SEMAPHORE:
        cn = translate_body(text)
    # translation quality check (guards against the LLM echoing English back):
    # accept directly when the Chinese ratio > 0.1; for table-heavy originals
    # (mostly quote/data tables, whose translation naturally has a low Chinese
    # ratio) relax to "Chinese share improved 30%+ over the original"
    cn_ratio = len(_CJK.findall(cn)) / len(cn)
    src_ratio = len(_CJK.findall(text)) / len(text)
    if len(cn.strip()) > 100 and (cn_ratio > 0.1 or cn_ratio >= src_ratio * 1.3):
        with PgClient() as pg:
            n = pg.execute(
                "UPDATE fin.report_meta SET content_text_cn=%s WHERE report_id=%s "
                "AND content_text_cn IS NULL", (cn, report_id))
        return n > 0
    logger.warning(f"#{report_id} 译文质量异常 (仍英文或过短), 未入库")
    return False


def spawn_translate(report_id: int) -> None:
    """Background translation on the ingestion path (fire-and-forget, failures only logged)"""
    def _job():
        try:
            if translate_report(report_id):
                logger.info(f"#{report_id} 正文中文翻译完成")
        except Exception as e:
            logger.warning(f"#{report_id} 正文翻译失败: {e}")

    threading.Thread(target=_job, daemon=True,
                     name=f"translate-{report_id}").start()


def backfill(limit: int = 0, workers: int = 3) -> None:
    """Backfill translation for existing rows: all English-dominant bodies without a translation"""
    from storage.pg_schema import ensure_report_analysis_columns
    ensure_report_analysis_columns()
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT report_id, left(content_text, 2000) head FROM fin.report_meta "
            "WHERE content_chars > 200 AND content_text_cn IS NULL")
    todo = [r["report_id"] for r in rows if is_en_text(r["head"])]
    if limit:
        todo = todo[:limit]
    logger.info(f"正文待补译 {len(todo)} 篇 (workers={workers})")

    def _safe(rid: int) -> bool:
        # a single failure (timeout ceiling/gateway jitter) is only logged, the
        # whole batch continues
        try:
            return translate_report(rid)
        except Exception as e:
            logger.warning(f"#{rid} 补译异常跳过: {str(e)[:80]}")
            return False

    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ok in ex.map(_safe, todo):
            done += 1 if ok else 0
    print(f"补译完成: {done}/{len(todo)} 篇写入 content_text_cn (失败篇目可重跑本命令续译)")


if __name__ == "__main__":
    import argparse
    from core.config import get_config
    get_config()  # load .env into the process environment (prerequisite for storage/LLM standalone use)
    ap = argparse.ArgumentParser(description="研报正文存量补译 (英文→content_text_cn)")
    ap.add_argument("--limit", type=int, default=0, help="最多处理篇数 (0=全部)")
    ap.add_argument("--workers", type=int, default=3, help="并发翻译线程数")
    a = ap.parse_args()
    backfill(a.limit, a.workers)
