# -*- encoding: utf-8 -*-
"""
研报正文中文翻译 (英文原版 → report_meta.content_text_cn)

- 入库链路: 深度提取完成后 spawn_translate() 起后台线程 (与链抽取同模式, 失败不影响主链路);
- 翻译范围: 正文前 _MAX_SRC_CHARS 字符, 与深度提取窗口对齐 (前端详情页只展示前 8000 字,
  超长原文的尾部译文对阅读无增益, 控制成本);
- 中文主导正文不翻译 (content_text_cn 留空, 读取侧回退原文);
- 存量补译: python -m skills.report.translate [--limit N] [--workers 3]
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

# 与 skill._MAX_TEXT_CHARS 对齐的翻译窗口 (源文字符)
_MAX_SRC_CHARS = 12000
# 单片送译上限 (英文 chars → 中文输出 token 约 1:1.2, 需给思考留预算)
_CHUNK_CHARS = 6000

# 翻译并发限流: LLM 网关同时最多 3 路
_TRANSLATE_SEMAPHORE = threading.BoundedSemaphore(3)

_CJK = re.compile(r"[\u4e00-\u9fff]")


def is_en_text(s: Optional[str], min_len: int = 20) -> bool:
    """CJK 字符占比 < 0.3 视为英文主导 (与元数据修复口径一致)"""
    s = str(s or "").strip()
    if len(s) < min_len:
        return False
    return len(_CJK.findall(s)) / len(s) < 0.3


def _split_chunks(text: str, size: int = _CHUNK_CHARS) -> List[str]:
    """按段落边界切分, 每片 ≤ size 字符; 单行超长 (表格线性化) 硬切"""
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
    """整段正文 (调用方保证已截窗) 分片串行翻译, 拼接返回"""
    from harness.llm import get_extract_llm
    llm = get_extract_llm(timeout=180.0)
    out: List[str] = []
    for chunk in _split_chunks(text):
        resp = (llm.invoke(TRANSLATE_PROMPT.format(text=chunk)) or "").strip()
        if resp.startswith("```"):  # 宽容剥壳
            resp = resp.strip("`").strip()
            if resp.startswith("text"):
                resp = resp[4:]
        out.append(resp)
    return "\n".join(out)


def translate_report(report_id: int) -> bool:
    """单篇补译: 英文主导且无译文的正文 → content_text_cn; 返回是否写入"""
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT report_id, content_text, content_text_cn FROM fin.report_meta "
            "WHERE report_id=%s", (report_id,))
    if not row or row.get("content_text_cn"):
        return False
    text = (row.get("content_text") or "")[:_MAX_SRC_CHARS]
    if len(text.strip()) < 200 or not is_en_text(text):
        return False  # 中文正文/过短/已译: 幂等跳过
    with _TRANSLATE_SEMAPHORE:
        cn = translate_body(text)
    # 译文须中文主导且非空 (防 LLM 原样吐回英文)
    if len(cn.strip()) > 100 and len(_CJK.findall(cn)) / len(cn) > 0.1:
        with PgClient() as pg:
            n = pg.execute(
                "UPDATE fin.report_meta SET content_text_cn=%s WHERE report_id=%s "
                "AND content_text_cn IS NULL", (cn, report_id))
        return n > 0
    logger.warning(f"#{report_id} 译文质量异常 (仍英文或过短), 未入库")
    return False


def spawn_translate(report_id: int) -> None:
    """入库链路后台翻译 (fire-and-forget, 失败只记日志)"""
    def _job():
        try:
            if translate_report(report_id):
                logger.info(f"#{report_id} 正文中文翻译完成")
        except Exception as e:
            logger.warning(f"#{report_id} 正文翻译失败: {e}")

    threading.Thread(target=_job, daemon=True,
                     name=f"translate-{report_id}").start()


def backfill(limit: int = 0, workers: int = 3) -> None:
    """存量补译: 英文主导且无译文的全部正文"""
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
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        for ok in ex.map(translate_report, todo):
            done += 1 if ok else 0
    print(f"补译完成: {done}/{len(todo)} 篇写入 content_text_cn")


if __name__ == "__main__":
    import argparse
    from core.config import get_config
    get_config()  # .env 灌入进程环境 (storage/LLM 独立使用前置)
    ap = argparse.ArgumentParser(description="研报正文存量补译 (英文→content_text_cn)")
    ap.add_argument("--limit", type=int, default=0, help="最多处理篇数 (0=全部)")
    ap.add_argument("--workers", type=int, default=3, help="并发翻译线程数")
    a = ap.parse_args()
    backfill(a.limit, a.workers)
