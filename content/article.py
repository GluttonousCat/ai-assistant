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


def _render_charts(profile: Dict) -> Dict[str, str]:
    """画像板块 -> 配图 (相对路径占位符; 失败的图返回占位说明文本)"""
    from content import charts
    s = profile.get("sections", {})
    ts_code = profile.get("ts_code") or ""
    name = profile.get("name", "")
    out = {}

    def _placeholder(key: str, path) -> str:
        return f"![](assets/{Path(path).name})" if path else "*(该配图数据不足, 跳过)*"

    try:
        out["img_mainbiz"] = _placeholder("mainbiz",
            charts.mainbiz_chart(s.get("main_business") or {}, ts_code))
        out["img_growth"] = _placeholder("growth",
            charts.growth_chart(s.get("financials") or {}, ts_code))
        out["img_margin"] = _placeholder("margin",
            charts.margin_chart(s.get("financials") or {}, ts_code))
        out["img_valuation"] = _placeholder("valuation",
            charts.valuation_chart(s.get("valuation") or {}, ts_code))
        out["img_industry"] = _placeholder("industry",
            charts.industry_chart(s.get("industry") or {}, ts_code, name))
    except Exception as e:  # noqa: BLE001 单图失败不挡成文
        logger.warning(f"配图生成部分失败: {e}")
        for k in ("img_mainbiz", "img_growth", "img_margin",
                  "img_valuation", "img_industry"):
            out.setdefault(k, "*(配图生成失败, 此处无图)*")
    return out


def write_stock_article(stock: str, years: int = 5,
                        annual_year: Optional[int] = None,
                        use_annual: bool = True) -> Dict[str, Any]:
    """画像 + 年报语料 -> 配图 -> 公众号文章 (六模块, 图文混排)。

    年报语料 (cninfo PDF 提炼) 全程降级: 无 PDF/无文本层/提炼失败均不阻塞成文。
    """
    from llm.client import get_agent_llm
    profile = build_company_profile(stock, years)
    digest = profile_digest(profile)
    if not profile.get("ok_sections"):
        raise RuntimeError(f"画像组装失败: {profile.get('failed_sections')}")
    imgs = _render_charts(profile)

    annual_block = "(无语料: 按上方量化画像成文)"
    if use_annual:
        try:
            from content.annual_report import annual_digest
            annual = annual_digest(profile.get("ts_code", ""),
                                   profile.get("name", stock), annual_year)
            if annual:
                annual_block = (f"### {annual['year']} 年度报告要点 (管理层讨论与"
                                f"风险章节提炼, 中文转述)\n{annual['digest']}")
        except Exception as e:  # noqa: BLE001 语料单源失败不挡主链路
            logger.warning(f"年报语料获取失败, 降级: {e}")

    prompt = GZH_STOCK_ARTICLE_PROMPT.format(
        digest=digest, annual_report=annual_block, disclaimer=DISCLAIMER,
        **imgs)
    article = get_agent_llm().invoke(prompt)
    path = _save(article, "gzh", profile.get("name", stock))
    n_imgs = sum(1 for v in imgs.values() if v.startswith("![]"))
    logger.info(f"公众号文章已生成: {path} ({len(article)} 字, {n_imgs} 配图, "
                f"年报语料: {'有' if '年度报告要点' in annual_block else '无'})")
    return {"path": str(path), "chars": len(article),
            "charts": n_imgs,
            "annual_report": "有" if "年度报告要点" in annual_block else "无",
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
