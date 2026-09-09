# -*- encoding: utf-8 -*-
"""
公众号文章生成: 股票画像模式 / 研报主题模式

产出 output/articles/YYYYMMDD_<slug>.md (公众号编辑器可直接粘贴 markdown)。
发布本身人工操作 (微信草稿箱 API 属后续可选接入, 涉及凭据与外发确认)。
"""
from __future__ import annotations

import re
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional

from core.logger import get_logger
from content.profile import build_company_profile, profile_digest
from content.prompts import (DISCLAIMER, GZH_STOCK_ARTICLE_PROMPT,
                             GZH_TOPIC_ARTICLE_PROMPT)

logger = get_logger(__name__)

ARTICLE_ROOT = Path("output/articles")


def _slug(text: str) -> str:
    """文件名安全片段: 保留中英数, 其余转 -"""
    s = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-")
    return s[:24] or "article"


def _save(markdown: str, kind: str, key: str) -> Path:
    ARTICLE_ROOT.mkdir(parents=True, exist_ok=True)
    path = ARTICLE_ROOT / f"{date.today():%Y%m%d}_{kind}_{_slug(key)}.md"
    path.write_text(markdown, encoding="utf-8")
    return path


def write_stock_article(stock: str, years: int = 5) -> Dict[str, Any]:
    """画像 -> 公众号文章。返回 {path, chars, digest_ok_sections, preview}"""
    from llm.client import get_agent_llm
    profile = build_company_profile(stock, years)
    digest = profile_digest(profile)
    if not profile.get("ok_sections"):
        raise RuntimeError(f"画像组装失败: {profile.get('failed_sections')}")

    prompt = GZH_STOCK_ARTICLE_PROMPT.format(digest=digest, disclaimer=DISCLAIMER)
    article = get_agent_llm().invoke(prompt)
    path = _save(article, "gzh", profile.get("name", stock))
    logger.info(f"公众号文章已生成: {path} ({len(article)} 字)")
    return {"path": str(path), "chars": len(article),
            "ok_sections": profile.get("ok_sections"),
            "preview": article[:400]}


def write_topic_article(topic: str, days: int = 90, limit: int = 8) -> Dict[str, Any]:
    """研报主题综述: 检索 -> 取观点 -> 成文。合规: 观点转述+标注机构, 不搬运原文"""
    import json
    from llm.client import get_agent_llm
    from mcp import get_registry
    registry = get_registry()

    env = registry.call("search_reports", {"keyword": topic, "days": days,
                                           "limit": limit})
    if not env.get("ok") or not env["data"].get("reports"):
        raise RuntimeError(f"主题「{topic}」未检索到研报")
    reports = env["data"]["reports"][:5]

    blocks = []
    for r in reports:
        det = registry.call("read_report", {"report_id": r["report_id"],
                                            "excerpt_chars": 3000})
        body = ""
        if det.get("ok"):
            body = (det["data"].get("content_excerpt") or "")[:2500]
            view = (det["data"].get("analysis") or {}).get("core_view") or ""
            body = (view + "\n" + body).strip()
        blocks.append(f"### {r['title']}\n机构: {r['org']} | 日期: {r['publish_date']} "
                      f"| 评级: {r.get('rating') or '—'}\n{body}")

    prompt = GZH_TOPIC_ARTICLE_PROMPT.format(
        topic=topic, reports="\n\n".join(blocks), disclaimer=DISCLAIMER)
    article = get_agent_llm().invoke(prompt)
    path = _save(article, "topic", topic)
    logger.info(f"主题文章已生成: {path} ({len(article)} 字)")
    return {"path": str(path), "chars": len(article),
            "reports_used": [r["title"] for r in reports],
            "preview": article[:400]}
