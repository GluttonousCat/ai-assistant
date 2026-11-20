# -*- encoding: utf-8 -*-
"""Report MCP adapters for research retrieval, forecasts and document tools."""

from __future__ import annotations

from tools.finance import data as finance_data, report_catalog
import json
from typing import Any, Dict

from harness.mcp.registry import REGISTRY
from harness.mcp.spec import ToolError, obj_schema, param
from harness.mcp.tools._common import _jsonable, resolve_one
from skills.report.attribution import lead_evidence, reported_rating
from skills.report.document import load_document, reading_status
from skills.report.answers import comparison_passages, indexed_original_quote


def _brief(row) -> Dict[str, Any]:
    """report_meta row -> concise summary."""
    rating = None
    rating_source = None
    lead = None
    material_type = "unknown"
    try:
        aj = json.loads(row.get("analysis_json") or "{}")
        rating = reported_rating(aj)
        rating_source = aj.get("rating_source")
        lead = lead_evidence(aj)
        material_type = aj.get("material_type") or "unknown"
    except Exception:
        pass
    return {
        "report_id": row["report_id"],
        "title": row["title"],
        "org": row["org_name"],
        "publish_date": _jsonable(row.get("publish_date")),
        "market": row.get("market"),
        "target": row.get("target"),
        "rating": rating,
        "rating_source": rating_source,
        "lead_evidence": lead,
        "material_type": material_type,
        "symbols": row.get("symbols"),
    }


@REGISTRY.tool(
    name="search_reports",
    domain="report",
    description=(
        "检索平台已入库的券商研报 (知识星球来源): 按关键词/股票标的/行业/商品主题匹配标题、"
        "标签与观点摘要, 返回标题、机构、日期、评级及材料性质。commentary 是二手解读，"
        "评级和观点须注明材料转述，unknown 表示材料性质待核实。"
        "用户问 '最近有哪些XX的研报'/'谁在写XX'/'XX行业观点' 时先调本工具, "
        "需要研报内容细节再用 read_report。只检索不解读。"
    ),
    params_schema=obj_schema(
        {
            "keyword": param(
                "关键词, 匹配标题/标签/观点 (如 'HBM'/'人形机器人')", "string"
            ),
            "stock": param("股票名或代码, 命中绑定标的 (可省)", "string"),
            "keywords": param("综述的多个检索词，一次查询按主题分别返回候选。与 keyword 二选一。",
                              "array", items={"type": "string"}, maxItems=6),
            "days": param("只看近 N 天, 0=不限", "integer", default=180),
            "limit": param("返回条数上限", "integer", default=10),
        }
    ),
    examples=["最近有哪些HBM的研报", "谁在写中际旭创", "人形机器人行业观点"],
)
def search_reports(
    keyword: str = "", stock: str = "", days: int = 180, limit: int = 10, keywords=None
) -> Dict[str, Any]:
    if keywords is not None:
        if (keyword or not isinstance(keywords, list) or not 1 <= len(keywords) <= 6
                or not all(isinstance(k, str) and 0 < len(k.strip()) <= 64 for k in keywords)):
            raise ToolError("keywords 需要 1 至 6 个非空检索词，请单独使用 keywords 或 keyword")
        keywords = list(dict.fromkeys(k.strip() for k in keywords))
    if not (keyword or stock or keywords):
        raise ToolError("请提供研报检索词或股票")
    hit = resolve_one(stock) if stock else None
    if keywords:
        rows = report_catalog.search_research("", hit, days, limit, keywords=keywords)
        return {"count": len({r["report_id"] for r in rows}), "reports": [
            {**_brief(r), "matched_keyword": r["matched_keyword"], "topic_rank": r["topic_rank"],
             "file_id": r["file_id"], "copy_key": r["copy_key"]} for r in rows]}
    rows = report_catalog.search_research(keyword, hit, days, limit)
    return {"count": len(rows), "reports": [_brief(r) for r in rows]}


@REGISTRY.tool(
    name="read_report",
    domain="report",
    description=(
        "读取单篇研报: 不带 question 返回元数据、观点来源及正文节选。用 summary_evidence 中的发言人、语气与引文解释观点，"
        "特别保留条件或限制，区分券商自己的判断、管理层指引及调查结果。"
        "带 question 时由 LLM 针对该篇研报正文做问答 (适合'这篇研报怎么看XX'类精确提问)。"
        "report_id 来自 search_reports 的结果。节选默认前 6000 字，body_status 与页数说明覆盖范围。"
        "legacy_unverified 是待核对的旧文字，仅供查找线索。"
    ),
    params_schema=obj_schema(
        {
            "report_id": param("研报 ID (search_reports 返回)", "integer"),
            "question": param("针对该篇的提问, 可选; 省略则返回原文节选", "string"),
            "focus": param("按主题定位原文片段，供多篇并行阅读；只读原件，无单篇模型调用。", "string"),
            "excerpt_chars": param("正文节选长度", "integer", default=6000),
        },
        ["report_id"],
    ),
    examples=["读一下 289 号研报", "这篇研报对光模块怎么看 (report_id=289)"],
)
def read_report(
    report_id: int, question: str = "", excerpt_chars: int = 6000, focus: str = ""
) -> Dict[str, Any]:
    row = report_catalog.report_body(report_id)
    if not row:
        raise ToolError(f"研报不存在: report_id={report_id}")

    base = _brief(row)
    base["industry"] = row.get("industry")
    try:
        analysis = json.loads(row.get("analysis_json") or "{}")
    except Exception:
        analysis = None

    if question:
        # Single-report Q&A: goes through ReportSkill qa mode (LLM, Chinese output)
        from skills.report.skill import ReportSkill
        from skills.base import SkillContext

        skill = ReportSkill()
        ctx = skill(
            SkillContext(
                user_input=question, params={"mode": "qa", "report_id": int(report_id)}
            )
        )
        if ctx.error:
            raise ToolError(f"研报问答失败: {ctx.error}")
        result = ctx.result or {}
        # The agent reads a bounded prefix of tool results. Keep the checked
        # answer ahead of the full historical analysis, which can fill that budget.
        return {"report_id": report_id, "answer": result.get("summary", ""),
                "citations": result.get("citations", []), "question": question, **base, "analysis": analysis}

    document = load_document(row)
    status = reading_status(row, document)
    if focus:
        index = analysis if isinstance(analysis, dict) else {}
        passages = comparison_passages(index, focus, document) if document.segments else []
        return {**base, **status, "indexed_quote": indexed_original_quote(index, document), "passages": [
            {"number": p.number, "page": p.page, "text": p.text} for p in passages]}
    # Keep archived, unlocated text distinguishable from original passages.
    # The catalog has already excluded pending attachments' posting commentary.
    if status["body_status"] == "legacy_unverified":
        content = row["content_text"]
    else:
        content = "\n\n".join(segment.render() for segment in document.segments)
    n = max(1000, min(int(excerpt_chars), 20000))
    return {
        **base, **status, "content_chars": len(content),
        "truncated": len(content) > n,
        "content_excerpt": content[:n],
        "analysis": analysis,
    }


@REGISTRY.tool(
    name="get_forecasts",
    domain="report",
    description=(
        "券商盈利预测 (未来期间): 某股票未来的营收/净利润/EPS 等预测值, "
        "按 (指标, 预测期间) 聚合出均值、机构数、分歧度(CV)与上修/下修方向, "
        "并列出各机构原始预测。用户问 '券商对XX明年利润的预期'/'预期分歧大不大'/"
        "'分析师在上调还是下调' 时调用。"
        "区别: 校验**历史**预测准不准用 verify_forecasts; 要预测+财务动量+估值的"
        "四象限综合判断用 analyze_alpha。"
    ),
    params_schema=obj_schema(
        {
            "stock": param("股票名或代码", "string"),
            "limit": param("聚合条数上限", "integer", default=6),
        },
        ["stock"],
    ),
    examples=[
        "券商对中际旭创明年的预期",
        "茅台的盈利预测分歧大吗",
        "宁德时代被上调还是下调",
    ],
)
def get_forecasts(stock: str, limit: int = 6) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from skills.beta_alpha.analysis.alpha_context import (
        forecast_divergence,
        report_views,
    )

    stats_rows, raw, view_rows = finance_data.forecast_context(
        hit["ts_code"], hit["name"]
    )
    stats = forecast_divergence(stats_rows)
    views = report_views(view_rows)
    forecasts = [{k: _jsonable(v) for k, v in r.items()} for r in raw]
    return {
        "stock": hit,
        "divergence_stats": stats[: int(limit)],
        "recent_forecasts": forecasts,
        "recent_views": views,
        "cv_hint": "CV>0.3 分歧较大, 0.15-0.3 中等, <0.15 一致预期",
    }


@REGISTRY.tool(
    name="fetch_annual_report",
    domain="report",
    description=(
        "从巨潮资讯网拉取上市公司定期报告原文 (年报/半年报/季报 PDF): 查询公告→"
        "元数据入库→下载 PDF 到本地。返回本地文件路径与公告元数据, "
        "配合 extract_document 可对年报全文提问。**有副作用 (写库+下载文件), "
        "只读模式下不可用**; 已下载的报告自动跳过 (幂等)。单份年报 PDF 数 MB, "
        "下载含反检测间隔约需 10~30 秒。"
    ),
    params_schema=obj_schema(
        {
            "stock": param("股票名或代码", "string"),
            "year": param(
                "报告年度, 如 2024 (该年年报于次年披露, 工具自动处理)", "integer"
            ),
            "category": param(
                "报告类别",
                "string",
                enum=["ndbg", "bndbg", "yjdbg", "sjdbg"],
                default="ndbg",
            ),
        },
        ["stock", "year"],
    ),
    examples=["拉取中际旭创2024年年报", "下载贵州茅台2024年报原文"],
    read_only=False,
    notes="内部走 tools/cninfo 爬虫 (orgId缓存+限流退避); 沙盒默认排除, "
    "需 openai_tools(include_write=True) 或 ALPHA_MCP_READ_ONLY=0 放开",
)
def fetch_annual_report(
    stock: str, year: int, category: str = "ndbg"
) -> Dict[str, Any]:
    from tools.cninfo import store
    from tools.cninfo.downloader import CninfoDownloader, DOWNLOAD_ROOT

    ts_code = resolve_one(stock)[
        "ts_code"
    ]  # name->code resolution stays on the agent side
    dl = CninfoDownloader()
    stats = dl.sync_stock(ts_code, [int(year)], download=True, categories=(category,))
    if stats.get("failed"):
        raise ToolError(
            f"巨潮拉取失败: {stock} ({ts_code}) {year} (站点限流时稍后重试)"
        )
    dest = (DOWNLOAD_ROOT / ts_code / f"{int(year)}_{category}.pdf").resolve()
    if dest.exists():
        entry = next(
            (
                e
                for e in store.load_stock_meta(ts_code)["announcements"].values()
                if e.get("category") == category and e.get("report_year") == int(year)
            ),
            {},
        )
        return {
            "stock": stock,
            "ts_code": ts_code,
            "year": int(year),
            "category": category,
            "title": entry.get("title"),
            "announce_date": entry.get("announce_date"),
            "file_path": str(dest),
            "file_size_mb": round(dest.stat().st_size / 1e6, 1),
            "stats": stats,
            "next_hint": "可用 extract_document(file_path) 解析全文后提问",
        }
    return {
        "stock": stock,
        "ts_code": ts_code,
        "year": int(year),
        "category": category,
        "stats": stats,
        "note": "元数据已查询, PDF 可能未就绪 (站点限流/无该期报告), 可稍后重试",
    }


@REGISTRY.tool(
    name="extract_document",
    domain="report",
    description=(
        "解析**用户提供的本地文件**为纯文本: 支持 PDF/DOCX/TXT/MD。普通 PDF 走文本层抽取; "
        "无文本层的扫描件 (图片型 PDF) 自动走视觉 OCR (慢, 每页需调视觉模型, 上限 10 页)。"
        "区别: 平台已入库的研报不要用本工具, 用 read_report (report_id 来自 search_reports)。"
        "只返回文本, 不写库。"
    ),
    params_schema=obj_schema(
        {
            "file_path": param("文档绝对路径或项目相对路径", "string"),
            "ocr_if_image": param(
                "扫描件是否启用视觉OCR (慢)", "boolean", default=True
            ),
            "excerpt_chars": param("返回文本节选长度", "integer", default=4000),
        },
        ["file_path"],
    ),
    examples=["读一下 D:/downloads/某某研报.pdf"],
    notes="扫描件 OCR 每页一次视觉模型调用, 10 页约 1-2 分钟; 建议用户等待提示",
)
def extract_document(
    file_path: str, ocr_if_image: bool = True, excerpt_chars: int = 4000
) -> Dict[str, Any]:
    import os

    path = os.path.abspath(file_path)
    if not os.path.exists(path):
        raise ToolError(f"文件不存在: {path}")

    from tools.finance.report_extractor import ReportExtractor

    text = ReportExtractor().extract(path)
    used_ocr = False

    if (not text or len(text.strip()) < 200) and ocr_if_image:
        from tools.pdf import is_image_pdf, ocr_pdf
        from skills.report.prompts import VISION_OCR_PROMPT
        from harness.llm import get_vision_llm

        if path.lower().endswith(".pdf") and is_image_pdf(path):
            text = (
                ocr_pdf(path, prompt=VISION_OCR_PROMPT, vision_llm=get_vision_llm())
                or ""
            )
            used_ocr = True
    if not text or not text.strip():
        raise ToolError(
            f"未能从 {os.path.basename(path)} 抽取到文本 (可能为扫描件且未开 OCR)"
        )
    n = max(500, min(int(excerpt_chars), 20000))
    return {
        "file": os.path.basename(path),
        "chars": len(text),
        "used_ocr": used_ocr,
        "excerpt": text[:n],
        "truncated": len(text) > n,
    }


@REGISTRY.tool(
    name="extract_pdf_tables",
    domain="report",
    description=(
        "提取 **PDF 文件里的线框表格** 为结构化行列 (财务表/股东表/明细表等)。"
        "走 pdfplumber 解析表格线, 不调用大模型, 速度快、数字精确。"
        "只处理有边框线的表格; 无框线的视觉排版表格不适用。"
        "适合: 年报/季报里的财务摘要表、十大股东表、募投项目表。"
    ),
    params_schema=obj_schema(
        {
            "file_path": param("PDF 绝对路径或项目相对路径", "string"),
            "max_pages": param("最多处理前 N 页", "integer", default=20),
            "max_tables": param("最多返回表格数", "integer", default=15),
        },
        ["file_path"],
    ),
    examples=[
        "提取 output/cninfo/downloads/688981.SH/2025_sjdbg.pdf 里的表格",
        "把这个年报 PDF 的财务表抽出来",
    ],
    notes="返回每张表的页码与行列; 无线框表格时返回空列表 (可改用 extract_document 的 OCR)",
)
def extract_pdf_tables(
    file_path: str, max_pages: int = 20, max_tables: int = 15
) -> Dict[str, Any]:
    import os

    path = os.path.abspath(file_path)
    if not os.path.exists(path):
        raise ToolError(f"文件不存在: {path}")
    if not path.lower().endswith(".pdf"):
        raise ToolError("只支持 PDF 文件")

    from tools.pdf import extract_tables

    tables = extract_tables(path, max_pages=max_pages)[:max_tables]
    return {
        "file": os.path.basename(path),
        "tables": len(tables),
        "data": [
            {"page": t["page"], "rows": len(t["rows"]), "preview": t["rows"][:6]}
            for t in tables
        ],
        "full": tables,
    }


@REGISTRY.tool(
    name="summarize_sell_side",
    domain="report",
    description=(
        "卖方观点汇总: 近一年覆盖该股的券商数量、各家盈利预测/目标价聚合(均值与区间)、"
        "最新预测明细。用户问 '券商怎么看XX'/'XX的目标价是多少'/'卖方一致预期' 时调用。"
    ),
    params_schema=obj_schema(
        {
            "stock": param("股票名或代码", "string"),
            "days": param("回看天数", "integer", default=365),
        },
        ["stock"],
    ),
    examples=["券商怎么看中际旭创", "贵州茅台目标价一致预期"],
)
def summarize_sell_side(stock: str, days: int = 365) -> Dict[str, Any]:
    hit = resolve_one(stock)
    rows = finance_data.sell_side_forecasts(hit["ts_code"], days)
    if not rows:
        raise ToolError(f"{hit['name']} 近{days}天无卖方盈利预测")

    def _f(v):
        return float(v) if v is not None and hasattr(v, "quantize") else v

    orgs = sorted({r["org_name"] for r in rows if r["org_name"]})
    groups = {}
    for r in rows:
        if not r["forecast_period"] or not r["forecast_type"]:
            continue
        unit = (r["forecast_unit"] or "").strip()
        value = _f(r["forecast_value"])
        if unit and isinstance(value, (int, float)):
            key = (f"{r['forecast_period']}.{r['forecast_type']}", unit)
            groups.setdefault(key, []).append(value)
    summary = []
    for (key, unit), vals in groups.items():
        summary.append(
            {
                "period_type": key,
                "n_forecasts": len(vals),
                "mean": round(sum(vals) / len(vals), 2),
                "min": round(min(vals), 2),
                "max": round(max(vals), 2),
                "unit": unit,
            }
        )
    summary.sort(key=lambda s: (-s["n_forecasts"], s["period_type"], s["unit"]))
    detail = []
    for r in rows[:12]:
        detail.append(
            {
                "org": r["org_name"],
                "date": r["publish_date"].isoformat()
                if hasattr(r["publish_date"], "isoformat")
                else r["publish_date"],
                "title": (r["title"] or "")[:40],
                "forecast": f"{r['forecast_period']} {r['forecast_type']} "
                f"{_f(r['forecast_value'])} {r['forecast_unit'] or ''}".strip(),
            }
        )
    return {
        "stock": hit,
        "days": days,
        "org_count": len(orgs),
        "orgs": orgs[:20],
        "forecast_summary": summary,
        "latest_detail": detail,
    }
