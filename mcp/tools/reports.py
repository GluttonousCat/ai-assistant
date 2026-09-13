# -*- encoding: utf-8 -*-
"""
研报域工具 (4个):
- search_reports    研报检索 (关键词/标的/行业/商品/评级)
- read_report       读研报 (元数据+正文节选; 带 question 时对单篇做问答)
- get_forecasts     券商盈利预测 + 分歧度统计
- extract_document  本地文档解析 (PDF/DOCX/TXT; 扫描件走视觉OCR)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from mcp.registry import REGISTRY
from mcp.spec import ToolError, obj_schema, param
from mcp.tools._common import _jsonable, resolve_one


def _brief(row) -> Dict[str, Any]:
    """report_meta 行 -> 精简摘要"""
    rating = None
    try:
        aj = json.loads(row.get("analysis_json") or "{}")
        rating = aj.get("rating")
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
        "symbols": row.get("symbols"),
    }


# ============================================================
# 7. search_reports
# ============================================================

@REGISTRY.tool(
    name="search_reports",
    domain="report",
    description=(
        "检索平台已入库的券商研报 (知识星球来源): 按关键词/股票标的/行业/商品主题匹配标题、"
        "标签与观点摘要, 返回报告列表 (标题/机构/日期/评级/标的)。"
        "用户问 '最近有哪些XX的研报'/'谁在写XX'/'XX行业观点' 时先调本工具, "
        "需要研报内容细节再用 read_report。只检索不解读。"),
    params_schema=obj_schema({
        "keyword": param("关键词, 匹配标题/标签/观点 (如 'HBM'/'人形机器人')", "string"),
        "stock": param("股票名或代码, 命中绑定标的 (可省)", "string"),
        "days": param("只看近 N 天, 0=不限", "integer", default=180),
        "limit": param("返回条数上限", "integer", default=10),
    }),
    examples=["最近有哪些HBM的研报", "谁在写中际旭创", "人形机器人行业观点"],
)
def search_reports(keyword: str = "", stock: str = "", days: int = 180,
                   limit: int = 10) -> Dict[str, Any]:
    if not keyword and not stock:
        raise ToolError("keyword 与 stock 至少提供一个")
    conds, args = [], []

    if stock:
        hit = resolve_one(stock)
        conds.append("(rm.ts_code=%s OR rm.title ILIKE %s OR rm.tags_targets ILIKE %s)")
        name = hit["name"]
        args += [hit["ts_code"], f"%{name}%", f"%{name}%"]

    if keyword:
        kw = f"%{keyword}%"
        conds.append("(rm.title ILIKE %s OR rm.tags_industries ILIKE %s "
                     "OR rm.tags_themes ILIKE %s OR rm.tags_commodities ILIKE %s "
                     "OR rm.target ILIKE %s OR rm.analysis_json ILIKE %s)")
        args += [kw, kw, kw, kw, kw, kw]

    if days and days > 0:
        conds.append("rm.publish_date >= current_date - %s")
        args.append(int(days))

    where = " AND ".join(conds) or "TRUE"
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            f"SELECT report_id, title, org_name, publish_date, market, target, "
            f"       symbols, tags_targets, analysis_json "
            f"FROM fin.report_meta rm WHERE {where} "
            f"ORDER BY rm.publish_date DESC NULLS LAST LIMIT %s",
            tuple(args) + (min(int(limit), 30),))
    return {"count": len(rows), "reports": [_brief(r) for r in rows]}


# ============================================================
# 8. read_report
# ============================================================

@REGISTRY.tool(
    name="read_report",
    domain="report",
    description=(
        "读取单篇研报: 不带 question 返回报告元数据+核心观点+正文节选 (供 Agent 自己综合); "
        "带 question 时由 LLM 针对该篇研报正文做问答 (适合'这篇研报怎么看XX'类精确提问)。"
        "report_id 来自 search_reports 的结果。正文节选默认前 6000 字。"),
    params_schema=obj_schema({
        "report_id": param("研报 ID (search_reports 返回)", "integer"),
        "question": param("针对该篇的提问, 可选; 省略则返回原文节选", "string"),
        "excerpt_chars": param("正文节选长度", "integer", default=6000),
    }, ["report_id"]),
    examples=["读一下 289 号研报", "这篇研报对光模块怎么看 (report_id=289)"],
)
def read_report(report_id: int, question: str = "",
                excerpt_chars: int = 6000) -> Dict[str, Any]:
    from storage.pg import PgClient
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT report_id, title, org_name, publish_date, market, target, "
            "       industry, content_text, analysis_json "
            "FROM fin.report_meta WHERE report_id=%s", (int(report_id),))
    if not row:
        raise ToolError(f"研报不存在: report_id={report_id}")

    base = _brief(row)
    base["industry"] = row.get("industry")
    try:
        base["analysis"] = json.loads(row.get("analysis_json") or "{}")
    except Exception:
        base["analysis"] = None

    content = row.get("content_text") or ""
    if question:
        # 单篇问答: 走 ReportSkill qa 模式 (LLM, 输出中文)
        from skills.report.skill import ReportSkill
        from skills.base import SkillContext
        skill = ReportSkill()
        ctx = skill(SkillContext(user_input=question,
                                 params={"mode": "qa", "report_id": int(report_id)}))
        if ctx.error:
            raise ToolError(f"研报问答失败: {ctx.error}")
        result = ctx.result or {}
        return {**base, "question": question, "answer": result.get("summary", "")}

    if not content:
        raise ToolError(
            f"研报 {report_id} 无正文文本 (可能未提取或为扫描件)。"
            f"可尝试 extract_document 或等 OCR 完成")
    n = max(1000, min(int(excerpt_chars), 20000))
    return {**base, "content_chars": len(content),
            "content_excerpt": content[:n],
            "truncated": len(content) > n}


# ============================================================
# 9. get_forecasts
# ============================================================

@REGISTRY.tool(
    name="get_forecasts",
    domain="report",
    description=(
        "券商盈利预测 (未来期间): 某股票未来的营收/净利润/EPS 等预测值, "
        "按 (指标, 预测期间) 聚合出均值、机构数、分歧度(CV)与上修/下修方向, "
        "并列出各机构原始预测。用户问 '券商对XX明年利润的预期'/'预期分歧大不大'/"
        "'分析师在上调还是下调' 时调用。"
        "区别: 校验**历史**预测准不准用 verify_forecasts; 要预测+财务动量+估值的"
        "四象限综合判断用 analyze_alpha。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "limit": param("聚合条数上限", "integer", default=6),
    }, ["stock"]),
    examples=["券商对中际旭创明年的预期", "茅台的盈利预测分歧大吗", "宁德时代被上调还是下调"],
)
def get_forecasts(stock: str, limit: int = 6) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from skills.beta_alpha.skills.alpha import (
        _load_forecast_divergence, _load_views,
    )
    from storage.pg import PgClient
    with PgClient() as pg:
        stats = _load_forecast_divergence(pg, hit["ts_code"])
        raw = pg.fetch_all(
            "SELECT rf.forecast_type, rf.forecast_period, rf.forecast_value, "
            "       rf.forecast_unit, rf.confidence, rm.org_name, rm.publish_date, "
            "       rm.title, rm.report_id "
            "FROM fin.report_forecast rf "
            "LEFT JOIN fin.report_meta rm ON rm.report_id = rf.report_id "
            "WHERE rf.ts_code=%s AND rf.forecast_value IS NOT NULL "
            "ORDER BY rf.forecast_period DESC, rm.publish_date DESC "
            "LIMIT %s", (hit["ts_code"], 20))
        views = _load_views(pg, hit["ts_code"], hit["name"])
    forecasts = [{k: _jsonable(v) for k, v in r.items()} for r in raw]
    return {"stock": hit, "divergence_stats": stats[:int(limit)],
            "recent_forecasts": forecasts, "recent_views": views,
            "cv_hint": "CV>0.3 分歧较大, 0.15-0.3 中等, <0.15 一致预期"}


# ============================================================
# 10b. fetch_annual_report (巨潮爬虫, 写类: 入库+下载文件, 默认不进对话沙盒)
# ============================================================

@REGISTRY.tool(
    name="fetch_annual_report",
    domain="report",
    description=(
        "从巨潮资讯网拉取上市公司定期报告原文 (年报/半年报/季报 PDF): 查询公告→"
        "元数据入库→下载 PDF 到本地。返回本地文件路径与公告元数据, "
        "配合 extract_document 可对年报全文提问。**有副作用 (写库+下载文件), "
        "只读模式下不可用**; 已下载的报告自动跳过 (幂等)。单份年报 PDF 数 MB, "
        "下载含反检测间隔约需 10~30 秒。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "year": param("报告年度, 如 2024 (该年年报于次年披露, 工具自动处理)", "integer"),
        "category": param("报告类别", "string",
                          enum=["ndbg", "bndbg", "yjdbg", "sjdbg"],
                          default="ndbg"),
    }, ["stock", "year"]),
    examples=["拉取中际旭创2024年年报", "下载贵州茅台2024年报原文"],
    read_only=False,
    notes="内部走 tools/cninfo 爬虫 (orgId缓存+限流退避); 沙盒默认排除, "
          "需 openai_tools(include_write=True) 或 ALPHA_MCP_READ_ONLY=0 放开",
)
def fetch_annual_report(stock: str, year: int, category: str = "ndbg") -> Dict[str, Any]:
    from tools.cninfo.downloader import CninfoDownloader
    dl = CninfoDownloader()
    stats = dl.sync_stock(stock, [int(year)], download=True,
                          categories=(category,))
    if stats.get("failed"):
        raise ToolError(f"巨潮拉取失败: {stock} {year} (站点限流时稍后重试)")
    from storage.pg import PgClient
    with PgClient() as pg:
        ts_code = None
        try:
            from mcp.tools._common import resolve_one
            ts_code = resolve_one(stock)["ts_code"]
        except ToolError:
            pass
        if ts_code:
            row = pg.fetch_one(
                "SELECT title, announce_date, file_path, file_size "
                "FROM fin.cninfo_announcement "
                "WHERE ts_code=%s AND category=%s AND report_year=%s "
                "AND download_status='done' LIMIT 1",
                (ts_code, category, int(year)))
            if row:
                return {"stock": stock, "year": int(year),
                        "category": category, "title": row["title"],
                        "announce_date": _jsonable(row["announce_date"]),
                        "file_path": row["file_path"],
                        "file_size_mb": round((row["file_size"] or 0) / 1e6, 1),
                        "stats": stats,
                        "next_hint": "可用 extract_document(file_path) 解析全文后提问"}
    return {"stock": stock, "year": int(year), "category": category,
            "stats": stats,
            "note": "元数据已入库, PDF 可能未就绪 (站点限流/无该期报告), "
                    "可稍后重试或查 fin.cninfo_announcement"}


# ============================================================
# 10. extract_document
# ============================================================

@REGISTRY.tool(
    name="extract_document",
    domain="report",
    description=(
        "解析**用户提供的本地文件**为纯文本: 支持 PDF/DOCX/TXT/MD。普通 PDF 走文本层抽取; "
        "无文本层的扫描件 (图片型 PDF) 自动走视觉 OCR (慢, 每页需调视觉模型, 上限 10 页)。"
        "区别: 平台已入库的研报不要用本工具, 用 read_report (report_id 来自 search_reports)。"
        "只返回文本, 不写库。"),
    params_schema=obj_schema({
        "file_path": param("文档绝对路径或项目相对路径", "string"),
        "ocr_if_image": param("扫描件是否启用视觉OCR (慢)", "boolean", default=True),
        "excerpt_chars": param("返回文本节选长度", "integer", default=4000),
    }, ["file_path"]),
    examples=["读一下 D:/downloads/某某研报.pdf"],
    notes="扫描件 OCR 每页一次视觉模型调用, 10 页约 1-2 分钟; 建议用户等待提示",
)
def extract_document(file_path: str, ocr_if_image: bool = True,
                     excerpt_chars: int = 4000) -> Dict[str, Any]:
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
        if path.lower().endswith(".pdf") and is_image_pdf(path):
            text = ocr_pdf(path, prompt=VISION_OCR_PROMPT) or ""
            used_ocr = True
    if not text or not text.strip():
        raise ToolError(f"未能从 {os.path.basename(path)} 抽取到文本 "
                        f"(可能为扫描件且未开 OCR)")
    n = max(500, min(int(excerpt_chars), 20000))
    return {"file": os.path.basename(path), "chars": len(text),
            "used_ocr": used_ocr, "excerpt": text[:n],
            "truncated": len(text) > n}


# ============================================================
# 11. extract_pdf_tables
# ============================================================

@REGISTRY.tool(
    name="extract_pdf_tables",
    domain="report",
    description=(
        "提取 **PDF 文件里的线框表格** 为结构化行列 (财务表/股东表/明细表等)。"
        "走 pdfplumber 解析表格线, 不调用大模型, 速度快、数字精确。"
        "只处理有边框线的表格; 无框线的视觉排版表格不适用。"
        "适合: 年报/季报里的财务摘要表、十大股东表、募投项目表。"),
    params_schema=obj_schema({
        "file_path": param("PDF 绝对路径或项目相对路径", "string"),
        "max_pages": param("最多处理前 N 页", "integer", default=20),
        "max_tables": param("最多返回表格数", "integer", default=15),
    }, ["file_path"]),
    examples=["提取 output/cninfo/downloads/688981.SH/2025_sjdbg.pdf 里的表格",
              "把这个年报 PDF 的财务表抽出来"],
    notes="返回每张表的页码与行列; 无线框表格时返回空列表 (可改用 extract_document 的 OCR)",
)
def extract_pdf_tables(file_path: str, max_pages: int = 20,
                       max_tables: int = 15) -> Dict[str, Any]:
    import os
    path = os.path.abspath(file_path)
    if not os.path.exists(path):
        raise ToolError(f"文件不存在: {path}")
    if not path.lower().endswith(".pdf"):
        raise ToolError("只支持 PDF 文件")

    from tools.pdf import extract_tables
    tables = extract_tables(path, max_pages=max_pages)[:max_tables]
    return {"file": os.path.basename(path),
            "tables": len(tables),
            "data": [{"page": t["page"],
                      "rows": len(t["rows"]),
                      "preview": t["rows"][:6]} for t in tables],
            "full": tables}
