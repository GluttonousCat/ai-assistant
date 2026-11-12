# -*- encoding: utf-8 -*-
"""
Report domain tools (7):
- search_reports       report search (keyword / target / industry / commodity / rating)
- read_report          read a report (metadata + body excerpt; with a question, Q&A over that single report)
- get_forecasts        broker earnings forecasts + divergence stats
- summarize_sell_side  sell-side view summary (covering orgs / forecast aggregation / target-price range)
- fetch_annual_report  cninfo periodic-report fetch (write: DB insert + PDF download)
- extract_document     local document parsing (PDF/DOCX/TXT; scanned docs go through vision OCR)
- extract_pdf_tables   PDF ruled-table extraction (pdfplumber, no model calls)
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from harness.mcp.registry import REGISTRY
from harness.mcp.spec import ToolError, obj_schema, param
from harness.mcp.tools._common import _jsonable, resolve_one


def _brief(row) -> Dict[str, Any]:
    """report_meta row -> concise summary."""
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
        # Single-report Q&A: goes through ReportSkill qa mode (LLM, Chinese output)
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
# 10b. fetch_annual_report (cninfo crawler; write tool: DB insert + file download; excluded from the conversation sandbox by default)
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
    from tools.cninfo import store
    from tools.cninfo.downloader import CninfoDownloader, DOWNLOAD_ROOT

    ts_code = resolve_one(stock)["ts_code"]   # name->code resolution stays on the agent side
    dl = CninfoDownloader()
    stats = dl.sync_stock(ts_code, [int(year)], download=True,
                          categories=(category,))
    if stats.get("failed"):
        raise ToolError(f"巨潮拉取失败: {stock} ({ts_code}) {year} "
                        f"(站点限流时稍后重试)")
    dest = (DOWNLOAD_ROOT / ts_code / f"{int(year)}_{category}.pdf").resolve()
    if dest.exists():
        entry = next((e for e in store.load_stock_meta(ts_code)
                      ["announcements"].values()
                      if e.get("category") == category
                      and e.get("report_year") == int(year)), {})
        return {"stock": stock, "ts_code": ts_code, "year": int(year),
                "category": category, "title": entry.get("title"),
                "announce_date": entry.get("announce_date"),
                "file_path": str(dest),
                "file_size_mb": round(dest.stat().st_size / 1e6, 1),
                "stats": stats,
                "next_hint": "可用 extract_document(file_path) 解析全文后提问"}
    return {"stock": stock, "ts_code": ts_code, "year": int(year),
            "category": category, "stats": stats,
            "note": "元数据已查询, PDF 可能未就绪 (站点限流/无该期报告), "
                    "可稍后重试"}


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
        from harness.llm import get_vision_llm
        if path.lower().endswith(".pdf") and is_image_pdf(path):
            text = ocr_pdf(path, prompt=VISION_OCR_PROMPT,
                           vision_llm=get_vision_llm()) or ""
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


# ============================================================
# 7. summarize_sell_side (sell-side view summary)
# ============================================================

@REGISTRY.tool(
    name="summarize_sell_side",
    domain="report",
    description=(
        "卖方观点汇总: 近一年覆盖该股的券商数量、各家盈利预测/目标价聚合(均值与区间)、"
        "最新预测明细。用户问 '券商怎么看XX'/'XX的目标价是多少'/'卖方一致预期' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "days": param("回看天数", "integer", default=365),
    }, ["stock"]),
    examples=["券商怎么看中际旭创", "贵州茅台目标价一致预期"],
)
def summarize_sell_side(stock: str, days: int = 365) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT r.org_name, r.publish_date, r.title, "
            "       f.forecast_type, f.forecast_period, f.forecast_value, "
            "       f.forecast_unit, f.confidence "
            "FROM fin.report_forecast f "
            "JOIN fin.report_meta r ON r.report_id = f.report_id "
            "WHERE f.ts_code=%s AND r.publish_date >= current_date - %s "
            "ORDER BY r.publish_date DESC LIMIT 300",
            (hit["ts_code"], int(days)))
    if not rows:
        raise ToolError(f"{hit['name']} 近{days}天无卖方盈利预测")

    def _f(v):
        return float(v) if v is not None and hasattr(v, "quantize") else v

    orgs = sorted({r["org_name"] for r in rows if r["org_name"]})
    groups: Dict[str, Dict[str, list]] = {}
    for r in rows:
        if not r["forecast_period"] or not r["forecast_type"]:
            continue
        key = f"{r['forecast_period']}.{r['forecast_type']}"
        groups.setdefault(key, {"vals": [], "units": set()}).setdefault(
            "units", set()).add(r["forecast_unit"] or "")
        groups[key]["vals"].append(_f(r["forecast_value"]))
    summary = []
    for key, g in groups.items():
        vals = [v for v in g["vals"] if isinstance(v, (int, float))]
        if not vals:
            continue
        summary.append({
            "period_type": key, "n_forecasts": len(vals),
            "mean": round(sum(vals) / len(vals), 2),
            "min": round(min(vals), 2), "max": round(max(vals), 2),
            "unit": (list(g["units"]) or [""])[0]})
    summary.sort(key=lambda s: (-s["n_forecasts"], s["period_type"]))
    detail = []
    for r in rows[:12]:
        detail.append({
            "org": r["org_name"],
            "date": r["publish_date"].isoformat()
            if hasattr(r["publish_date"], "isoformat") else r["publish_date"],
            "title": (r["title"] or "")[:40],
            "forecast": f"{r['forecast_period']} {r['forecast_type']} "
                        f"{_f(r['forecast_value'])} {r['forecast_unit'] or ''}".strip()})
    return {"stock": hit, "days": days, "org_count": len(orgs),
            "orgs": orgs[:20], "forecast_summary": summary,
            "latest_detail": detail}
