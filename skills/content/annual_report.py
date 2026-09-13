# -*- encoding: utf-8 -*-
"""
年报语料模块: cninfo 年报 PDF → 章节切片 → LLM 提炼要点

解读 Agent 的输入增量: 六模块文章的模块一(公司速览)/模块五(风险)吸收
管理层原文口径表述。年报为公司公开披露文件, 无版权问题, 但仍"提炼转述,
不整段搬运"。降级链: 无 PDF / 无文本层 / 章节定位失败 / 提炼失败 → None,
文章照常成文 (单源语料绝不阻塞主链路)。
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, Optional

from core.logger import get_logger

logger = get_logger(__name__)

_MAX_OPERATING_CHARS = 9000   # 管理层讨论切片上限
_MAX_RISK_CHARS = 6000        # 风险章节上限
_MAX_CORPUS_CHARS = 15000     # 送入 LLM 的语料上限

_DIGEST_PROMPT = """你是券商年报研究员。以下是{name}({ts_code}) {label}的
「管理层讨论与分析」与「风险」章节节选。提炼成中文要点, 严格按以下三段格式输出
(不编造节选中没有的数字, 引用数据保留原口径):

## 业务与经营要点
(3~5 条: 主营业务构成/产能产量销量/研发进展/客户与市场结构 — 只写节选里有的)
## 亮点与隐忧
(2~4 条: 管理层自己表述的增长动因 + 管理层自己承认的谨慎表述)
## 年报风险清单
(3~6 条: 市场/技术/供应链/财务等, 说人话)

年报节选:
{corpus}"""


def locate_annual_pdf(ts_code: str,
                      year: Optional[int] = None) -> Optional[Dict]:
    """在 fin.cninfo_announcement 定位最新已下载年报全文 PDF。
    返回 {year, path}; 无可用文件返回 None。"""
    rows = _locate_report_pdfs(ts_code, year)
    return ({"year": rows[0]["report_year"], "path": rows[0]["file_path"]}
            if rows else None)


def _locate_report_pdfs(ts_code: str,
                        year: Optional[int] = None) -> list:
    """年报+半年报候选 (报告期新→旧, 同期年报优先)。半年报与年报同构
    (管理层讨论与分析/风险章节), 年报为图片版无文本层时以半年报顶上。"""
    from storage.pg import PgClient
    try:
        with PgClient() as pg:
            return pg.fetch_all(
                """
                SELECT announcement_id, report_year, file_path, category
                FROM fin.cninfo_announcement
                WHERE ts_code=%s AND category=ANY('{ndbg,bndbg}')
                  AND download_status='done' AND file_path IS NOT NULL
                  AND (%s::int IS NULL OR report_year=%s)
                ORDER BY report_year DESC, category DESC
                """, (ts_code, year, year))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"报告定位查询失败: {e}")
        return []


def _pdf_text(path: str) -> str:
    """文本层提取 (fitz→pdfplumber→pypdf 降级链, 见 tools/pdf/text.py)"""
    from tools.pdf import extract_text
    return extract_text(path)


def _slice(text: str, pat: str, max_chars: int) -> str:
    """关键词定位章节: 第2次出现(跳过目录) → 到下一个「第X节」标记或上限。
    宽松定位即可 — 下游是 LLM 提炼, 容错高。"""
    hits = [m.start() for m in re.finditer(pat, text)]
    if not hits:
        return ""
    start = hits[1] if len(hits) > 1 else hits[0]
    seg = text[start:start + max_chars * 3]
    m = re.search(r"第[一二三四五六七八九十百]+节", seg[len(pat) + 10:])
    end = (m.start() + len(pat) + 10) if m else len(seg)
    return seg[:min(end, max_chars)]


def annual_digest(ts_code: str, name: str,
                  year: Optional[int] = None) -> Optional[Dict]:
    """定期报告 → 中文要点。返回 {year, label, digest} 或 None (全程降级不抛)。

    候选链: 年报 → 半年报 (图片版年报无文本层时以同结构的半年报顶上)。
    """
    cands = _locate_report_pdfs(ts_code, year)
    if not cands:
        logger.info(f"{ts_code} 无已下载年报/半年报 PDF, 语料降级跳过")
        return None
    text, loc = "", None
    for row in cands:
        if not Path(row["file_path"]).exists():
            continue
        text = _pdf_text(row["file_path"])
        if len(text.strip()) >= 2000:
            loc = row
            break
        logger.info(f"{ts_code} {row['report_year']} {row['category']} "
                    f"无文本层 ({len(text)} 字, 疑图片型), 试下一候选")
    if loc is None:
        logger.info(f"{ts_code} 全部候选报告均无文本层, 语料跳过 "
                    f"(图片版可后续挂 tools/pdf 的 ocr_pdf)")
        return None
    label = (f"{loc['report_year']} 年度报告" if loc["category"] == "ndbg"
             else f"{loc['report_year']} 半年度报告")
    operating = _slice(text, r"管理层讨论与分析", _MAX_OPERATING_CHARS)
    risk = (_slice(text, r"可能面对的风险", _MAX_RISK_CHARS)
            or _slice(text, r"风险因素", _MAX_RISK_CHARS))
    corpus = f"{operating}\n\n{risk}".strip()
    if len(corpus) < 500:
        logger.info(f"{ts_code} 报告章节定位失败 (全文 {len(text)} 字), 语料跳过")
        return None
    corpus = corpus[:_MAX_CORPUS_CHARS]

    from core.llm.client import get_extract_llm
    prompt = _DIGEST_PROMPT.format(name=name or ts_code, ts_code=ts_code,
                                   label=label, corpus=corpus)
    try:
        out = get_extract_llm().invoke(prompt)
    except Exception as e:  # noqa: BLE001
        logger.warning(f"报告要点提炼失败: {e}")
        return None
    if not out or len(out.strip()) < 50:
        return None
    logger.info(f"年报语料就绪: {ts_code} {label} "
                f"(全文 {len(text) // 1000}k 字 → 要点 {len(out)} 字)")
    return {"year": loc["report_year"], "label": label, "digest": out.strip()}
