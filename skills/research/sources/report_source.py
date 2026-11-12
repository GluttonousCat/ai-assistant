# -*- encoding: utf-8 -*-
"""
T04 · research-report view and claim extraction (Stage 1 evidence layer; task:
docs/research/tasks/T04_研报观点与主张抽取.md)

Organizes the report library's **views** on a company into a View shard
(output/research/<ts_code>/views_reports.json). A view is never a fact — org+publish_date
is a view's identity card (three-layer separation contract in skills/research/contracts).

Data path: goes only through the harness.mcp registry, direct SQL forbidden:
  search_reports (result list) -> read_report (per report: structured analysis preferred,
  single-report QA as fallback when the core view is missing, default cap 6 reports)
  -> get_forecasts (earnings-forecast time series + divergence, fills in table forecasts)

View categories (category):
  viewpoint  core view / key-point paraphrase (analysis.core_view / key_points)
  forecast   earnings forecast (period and value caliber mandatory; analysis.forecasts / fin.report_forecast)
  rating     rating / target price (analysis.rating / target_price forecast)
  risk       risk warnings (analysis.risks)

Conflicting views are all kept (multi-source cross-checking; the conflict itself is
information, no arbitration); org normalized via core.helpers.normalize_org_name
(English investment-bank names -> common Chinese names); vid = R:<report_id>.<seq>
(numbered within one report in core-view -> key-points -> rating/target-price ->
forecast -> risk order). Primary-subject detection (analysis.ts_codes first, with
title/bound-target/company-name fallbacks): a search may hit non-primary reports via
tags (e.g. a Wuliangye report appearing in a Moutai search); their views carry a
「【primary subject name】」prefix to mark attribution so ratings/forecast numbers are
never misattributed to the target company — the prefix only marks ownership, never
changes the original caliber.

CLI: python -m skills.research.sources.report_source 贵州茅台 [--days 365] [--qa 6]
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.helpers import normalize_org_name
from core.logger import get_logger
from skills.research.contracts import (
    EvidenceShard,
    View,
    research_dir,
    save_shard,
    validate_shard,
)

log = get_logger(__name__)

# Closed set of view categories (T04 task definition)
CATEGORIES = ("viewpoint", "forecast", "rating", "risk")

# earnings-forecast metric -> Chinese name (report_forecast.forecast_type / analysis.forecasts.metric)
_METRIC_CN = {
    "revenue": "营业收入",
    "net_profit": "净利润",
    "eps": "每股收益",
    "roe": "净资产收益率",
    "target_price": "目标价",
    "gross_margin": "毛利率",
}

# Question for the single-report QA fallback (read_report question parameter; asks for one
# labeled item per line, easing deterministic parsing)
_QA_QUESTION = (
    "请从这篇研报中提取观点并逐行输出, 每行一项, 格式为「标签: 内容」。"
    "标签只能取: 核心观点/评级/目标价/盈利预测/风险。"
    "其中盈利预测行写成「盈利预测: 期间|指标|数值|单位」(如 2026E|净利润|862|亿元)。"
    "研报未提及的标签不要输出, 不加任何解释, 全部用简体中文。"
)

_VID_RE = re.compile(r"^R:\d+\.\d+$")


# ---------- pure functions (offline testable) ----------

def _fmt_num(v: Any) -> str:
    """Number -> statement-friendly string (drop .0 on integral floats, else verbatim)"""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def norm_period(p: str) -> str:
    """Forecast-period normalization: 'NA'->''; '2026E' downgrades to '2026' while the
    contract's valid_period doesn't accept the E suffix (the estimate caliber survives in
    the statement's wording; if the contract later extends to YYYYE, E is kept automatically)."""
    p = (p or "").strip()
    if not p or p.upper() == "NA":
        return ""
    try:
        from skills.research.contracts import valid_period
        if valid_period(p):
            return p
    except ImportError:
        pass
    m = re.match(r"^(\d{4})E$", p, flags=re.I)
    return m.group(1) if m else p


def _forecast_parts(fc: Dict[str, Any]):
    """One analysis.forecasts entry -> (category, statement, period); if the caliber is
    incomplete -> (None,..) dropped. raw_text is the LLM's Chinese paraphrase stored at
    ingestion (period+value+unit self-contained); kept verbatim when present."""
    metric = str(fc.get("metric") or "").strip()
    period = norm_period(str(fc.get("period") or "").strip())
    value, unit = fc.get("value"), str(fc.get("unit") or "").strip()
    raw = str(fc.get("raw_text") or "").strip()
    is_tp = metric == "target_price"
    if not is_tp and not period and raw:
        pm = re.search(r"20\d{2}", raw)     # period missing: fall back to extracting a year from the paraphrase
        period = norm_period(pm.group(0)) if pm else ""
    if not is_tp and not period:
        return (None, "", "")               # a forecast must carry a period; drop otherwise
    if raw:
        return ("rating" if is_tp else "forecast", raw, "" if is_tp else period)
    if value is None or not unit:
        return (None, "", "")   # numeric caliber incomplete — better missing than wrong
    stmt = (f"目标价 {_fmt_num(value)}{unit}" if is_tp
            else f"预计 {period} {_METRIC_CN.get(metric, metric)}{_fmt_num(value)}{unit}")
    return ("rating" if is_tp else "forecast", stmt, "" if is_tp else period)


def _context_prefix(detail: Optional[Dict[str, Any]], brief: Dict[str, Any],
                    ts_code: str, name: str) -> str:
    """Primary-subject detection: a report may hit the search via tags rather than being a
    primary subject (e.g. a Wuliangye report inside a Moutai search — its rating/forecast
    numbers belong to Wuliangye). Primary-subject reports return ''; others return a
    statement prefix「【primary subject name】」— marking attribution prevents
    misattribution without changing the original caliber. Decision chain:
    analysis.ts_codes contains the target code -> primary; else title/bound-target/
    company-name contains the target name -> primary; undecidable ->「【关联提及】」."""
    detail = detail or {}
    analysis = detail.get("analysis") or {}
    bare = (ts_code or "").split(".")[0]
    ts_codes = [str(x).strip() for x in (analysis.get("ts_codes") or []) if str(x).strip()]
    cnames = [str(x).strip() for x in (analysis.get("company_names") or [])
              if str(x).strip()]
    if ts_codes:
        if any(x == ts_code or x == bare for x in ts_codes):
            return ""
        return f"【{cnames[0]}】" if cnames else "【关联提及】"
    title = str(detail.get("title") or brief.get("title") or "")
    target = str(brief.get("target") or "")
    if name and (name in title or name in cnames or (target and target == name)):
        return ""
    return f"【{cnames[0]}】" if cnames else "【关联提及】"


def build_views_from_report(detail: Optional[Dict[str, Any]], brief: Dict[str, Any],
                            ts_code: str = "", max_points: int = 4,
                            max_risks: int = 2, context_prefix: str = "") -> List[View]:
    """read_report response body (or just the search-list brief) -> that report's View
    list (vid left empty, assigned uniformly).

    Order: core view -> key points (truncated) -> rating -> forecasts (target price goes
    to rating) -> risks (truncated); within one report, duplicate (category, statement)
    dropped. org always normalized; context_prefix is the attribution prefix for
    non-primary-subject reports (see _context_prefix)."""
    detail = detail or {}
    rid = int(detail.get("report_id") or brief.get("report_id") or 0)
    org = normalize_org_name(str(detail.get("org") or brief.get("org") or ""))
    date = str(detail.get("publish_date") or brief.get("publish_date") or "")
    analysis = detail.get("analysis") or {}

    pieces: List[tuple] = []
    core = str(analysis.get("core_view") or "").strip()
    if core:
        pieces.append(("viewpoint", core, ""))
    for kp in (analysis.get("key_points") or [])[:max_points]:
        s = str(kp).strip()
        if s:
            pieces.append(("viewpoint", s, ""))
    rating = str(analysis.get("rating") or brief.get("rating") or "").strip()
    if rating and rating != "无":
        pieces.append(("rating", f"评级为「{rating}」", ""))
    for fc in (analysis.get("forecasts") or []):
        cat, stmt, period = _forecast_parts(fc if isinstance(fc, dict) else {})
        if cat:
            pieces.append((cat, stmt, period))
    for rk in (analysis.get("risks") or [])[:max_risks]:
        s = str(rk).strip()
        if s:
            pieces.append(("risk", s, ""))

    views, seen = [], set()
    for cat, stmt, period in pieces:
        if not stmt or not date or not org:
            continue    # a view without org/date loses its identity card; not emitted
        key = (cat, stmt)
        if key in seen:
            continue
        seen.add(key)
        views.append(View(vid="", statement=context_prefix + stmt, org=org,
                          publish_date=date, report_id=rid, layer="view",
                          ts_code=ts_code, period=period, category=cat))
    return views


def build_forecast_view(row: Dict[str, Any], ts_code: str = "") -> Optional[View]:
    """One get_forecasts.recent_forecasts row (report_forecast table) -> forecast/rating View"""
    try:
        rid = int(row.get("report_id") or 0)
    except (TypeError, ValueError):
        return None
    org = normalize_org_name(str(row.get("org_name") or ""))
    date = str(row.get("publish_date") or "")
    ftype = str(row.get("forecast_type") or "").strip()
    period = norm_period(str(row.get("forecast_period") or ""))
    value, unit = row.get("forecast_value"), str(row.get("forecast_unit") or "").strip()
    if not (rid and org and date and value is not None and unit):
        return None
    metric = _METRIC_CN.get(ftype, ftype or "指标")
    if ftype != "target_price" and not period:
        return None                         # a forecast must carry a period
    if ftype == "target_price":
        return View(vid="", statement=f"目标价 {_fmt_num(value)}{unit}", org=org,
                    publish_date=date, report_id=rid, layer="view", ts_code=ts_code,
                    period="", category="rating")
    return View(vid="", statement=f"预计 {period} {metric}{_fmt_num(value)}{unit}",
                org=org, publish_date=date, report_id=rid, layer="view",
                ts_code=ts_code, period=period, category="forecast")


# QA-fallback line labels -> category (longer labels first to prevent mis-matching)
_QA_LABELS = [("核心观点", "viewpoint"), ("盈利预测", "forecast"), ("目标价", "rating"),
              ("评级", "rating"), ("风险", "risk"), ("观点", "viewpoint"),
              ("预测", "forecast")]
_QA_LINE_RE = re.compile(
    r"^(核心观点|盈利预测|目标价|评级|风险|观点|预测)\s*[:：]\s*(.+)$")


def parse_qa_views(answer: str, report_id: int, org: str, publish_date: str,
                   ts_code: str = "", max_views: int = 6,
                   context_prefix: str = "") -> List[View]:
    """Single-report QA line-by-line answer -> View list (lenient parsing: strip
    bullets/numbering, skip not-mentioned lines)"""
    org = normalize_org_name(str(org or ""))
    views: List[View] = []
    if not org or not str(publish_date or "").strip():
        return views                        # a view without org/date loses its identity card; not emitted
    for line in (answer or "").splitlines():
        s = re.sub(r"^[\s\-–—*•·]+", "", line.strip())
        s = re.sub(r"^\d+\s*[.、)．]\s*", "", s, count=1).strip()
        if not s or "未提及" in s:
            continue
        m = _QA_LINE_RE.match(s)
        if not m:
            continue
        label, content = m.group(1), m.group(2).strip()
        if not content:
            continue
        if label == "目标价":             # keep the sentence self-contained, readable out of context
            content = f"目标价 {content}"
        elif label == "评级":
            content = f"评级为「{content}」"
        cat = dict(_QA_LABELS)[label]
        period = ""
        if cat == "forecast":
            parts = [p.strip() for p in content.split("|")]
            if len(parts) >= 4 and parts[2] and not parts[2].isspace():
                per, metric, val, unit = parts[0], parts[1], parts[2], parts[3]
                period = norm_period(per)
                content = f"预计 {per} {metric}{val}{unit}"
            else:
                pm = re.search(r"(20\d{2})\s*E?", content)
                period = norm_period(pm.group(0).replace(" ", "")) if pm else ""
        views.append(View(vid="", statement=context_prefix + content, org=org,
                          publish_date=str(publish_date or ""), report_id=int(report_id),
                          layer="view", ts_code=ts_code, period=period, category=cat))
        if len(views) >= max_views:
            break
    return views


def finalize_vids(views: List[View]) -> List[View]:
    """Assign vid = R:<report_id>.<seq> in list order (increments from 1 within a report, independent across reports)"""
    seq: Dict[int, int] = {}
    for v in views:
        rid = int(v.report_id or 0)
        seq[rid] = seq.get(rid, 0) + 1
        v.vid = f"R:{rid}.{seq[rid]}"
    return views


def validate_views(views: List[View]) -> List[str]:
    """T04 semantic self-check (category/caliber rules beyond T01's validate_shard);
    returns the problem list (empty = pass): category within the closed set; forecast
    must carry a period and its statement contain digits; vid format and uniqueness;
    statement/org non-empty."""
    problems: List[str] = []
    seen = set()
    for i, v in enumerate(views):
        tag = v.vid or f"views[{i}]"
        if not _VID_RE.match(v.vid or ""):
            problems.append(f"{tag}: vid 不符合 R:<report_id>.<seq>")
        if v.vid in seen:
            problems.append(f"{tag}: vid 重复")
        seen.add(v.vid)
        if v.category not in CATEGORIES:
            problems.append(f"{tag}: category 非法 {v.category!r} (闭集 {CATEGORIES})")
        if not (v.statement or "").strip():
            problems.append(f"{tag}: statement 为空")
        if not (v.org or "").strip():
            problems.append(f"{tag}: 缺 org (观点必带机构)")
        if v.category == "forecast":
            if not v.period:
                problems.append(f"{tag}: forecast 缺 period")
            if not re.search(r"\d", v.statement or ""):
                problems.append(f"{tag}: forecast statement 不含数值")
    return problems


# ---------- orchestration (via the MCP registry) ----------

def _mcp():
    from harness.mcp import get_registry
    return get_registry()


def _call(reg, name: str, **kwargs) -> Optional[Dict[str, Any]]:
    """Unified MCP call; log failures and return None (one report failing must not sink the batch)"""
    env = reg.call(name, {k: v for k, v in kwargs.items() if v is not None})
    if not env.get("ok"):
        log.warning("MCP %s(%s) 失败: %s", name, kwargs, env.get("error"))
        return None
    return env.get("data")


def _dup_forecast(views: List[View], rid: int, period: str, metric_cn: str) -> bool:
    """Whether a forecast of the same report+period+metric already exists (dup detection
    via the metric word inside the statement, compatible with raw_text paraphrases)"""
    return any(v.category == "forecast" and v.report_id == rid
               and v.period == period and metric_cn in v.statement for v in views)


def collect_report_views(stock: str, days: int = 365, out_dir: Path | None = None,
                         limit: int = 30, qa_fallback: int = 6) -> EvidenceShard:
    """Collect a stock's research-report views from the last N days -> a View shard
    persisted as views_reports.json.

    stock: stock name or code; days: look-back days; out_dir: output directory
    (default output/research/<ts_code>/); limit: search report-count cap (MCP cap 30);
    qa_fallback: cap on single-report QA fallbacks when structured analysis lacks a core
    view (0 = disabled).
    """
    reg = _mcp()

    fc = _call(reg, "get_forecasts", stock=stock, limit=6)
    if fc is None or not (fc.get("stock") or {}).get("ts_code"):
        raise ValueError(f"无法解析股票「{stock}」: {stock} 不在股票库或盈利预测工具不可用")
    ts_code = fc["stock"]["ts_code"]
    name = fc["stock"].get("name") or stock

    sr = _call(reg, "search_reports", stock=stock, days=int(days), limit=int(limit))
    briefs = (sr or {}).get("reports") or []

    views: List[View] = []
    reports_read, qa_used, non_subject = 0, 0, 0
    for b in briefs:
        rid = int(b.get("report_id") or 0)
        if not rid:
            continue
        detail = _call(reg, "read_report", report_id=rid, excerpt_chars=1000)
        prefix = _context_prefix(detail, b, ts_code, name)
        if prefix:
            non_subject += 1
        got = build_views_from_report(detail, brief=b, ts_code=ts_code,
                                      context_prefix=prefix)
        if detail is not None:
            reports_read += 1
            analysis = detail.get("analysis") or {}
            has_core = bool(str(analysis.get("core_view") or "").strip()
                            or (analysis.get("key_points") or []))
            if qa_fallback and qa_used < qa_fallback and not has_core:
                qa = _call(reg, "read_report", report_id=rid, question=_QA_QUESTION)
                if qa is not None:
                    qa_used += 1
                    got += parse_qa_views(qa.get("answer") or "", rid,
                                          detail.get("org") or b.get("org") or "",
                                          detail.get("publish_date")
                                          or b.get("publish_date") or "",
                                          ts_code=ts_code, context_prefix=prefix)
        views += got

    # report_forecast table fill-in: only fill same report+period+metric gaps (includes
    # older forecasts outside the search window)
    for row in fc.get("recent_forecasts") or []:
        v = build_forecast_view(row, ts_code=ts_code)
        if v is None:
            continue
        if v.category == "forecast":
            metric_cn = next((cn for k, cn in _METRIC_CN.items()
                              if k == str(row.get("forecast_type"))), "")
            if _dup_forecast(views, v.report_id, v.period, metric_cn):
                continue
        views.append(v)

    # wrap-up: dedupe by caliber within a report -> fill ts_code -> assign vids ->
    # dual contract/semantic validation
    dedup, seen = [], set()
    for v in views:
        if not v.ts_code:
            v.ts_code = ts_code
        key = (v.report_id, v.statement)
        if key in seen:
            continue
        seen.add(key)
        dedup.append(v)
    finalize_vids(dedup)

    shard = EvidenceShard(
        kind="reports", ts_code=ts_code, views=dedup,
        meta={
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            "stock": stock, "name": name, "days": int(days),
            "reports_searched": len(briefs), "reports_read": reports_read,
            "qa_fallback_used": qa_used, "non_subject_reports": non_subject,
            "orgs": sorted({v.org for v in dedup}),
            "category_counts": {c: sum(1 for v in dedup if v.category == c)
                                for c in CATEGORIES},
            "forecast_divergence": fc.get("divergence_stats") or [],
            "contract_errors": validate_shard(shard_probe(dedup, ts_code))
            if dedup else [],
            "semantic_errors": validate_views(dedup),
            "note": "观点层证据 — 引用必须标注机构+日期, 严禁当事实句 (三层分离)",
        })
    errors = shard.meta["contract_errors"] + shard.meta["semantic_errors"]
    if errors:
        log.warning("views 分片自检未过 (%d 项): %s", len(errors), errors[:5])

    if out_dir is None:
        out_dir = research_dir(Path(_output_root()), ts_code)
    out_dir = Path(out_dir)
    save_shard(shard, out_dir / "views_reports.json")
    shard.meta["output_path"] = str(out_dir / "views_reports.json")
    log.info("T04 %s: %d 条观点 (研报 %d 篇, 机构 %d 家) -> %s",
             ts_code, len(dedup), len(briefs),
             len(shard.meta["orgs"]), shard.meta["output_path"])
    return shard


def shard_probe(views: List[View], ts_code: str) -> EvidenceShard:
    """Assemble a temporary shard for the contract validator (used inside collect; avoids
    building the shard first and then mutating views)"""
    return EvidenceShard(kind="reports", ts_code=ts_code, views=views)


def _output_root() -> str:
    from core.paths import PathManager
    return PathManager().output_dir


# ---------- CLI ----------

def main(argv: Optional[List[str]] = None) -> None:
    import argparse
    p = argparse.ArgumentParser(
        prog="python -m skills.research.sources.report_source",
        description="T04 研报观点与主张抽取 — 生成 views_reports.json (View 分片)")
    p.add_argument("stock", help="股票名或代码, 如 贵州茅台 / 600519")
    p.add_argument("--days", type=int, default=365, help="回看天数 (默认 365)")
    p.add_argument("--limit", type=int, default=30, help="检索篇数上限 (默认 30, 上限 30)")
    p.add_argument("--qa", type=int, default=6,
                   help="analysis 缺核心观点时单篇问答兜底篇数上限 (默认 6, 0=关闭)")
    p.add_argument("--out", default=None, help="输出目录 (默认 output/research/<ts_code>)")
    args = p.parse_args(argv)

    shard = collect_report_views(args.stock, days=args.days,
                                 out_dir=Path(args.out) if args.out else None,
                                 limit=args.limit, qa_fallback=args.qa)
    m = shard.meta
    print(f"[T04] {shard.ts_code} ({m['name']}) 观点分片: {len(shard.views)} 条 views, "
          f"机构 {len(m['orgs'])} 家 ({', '.join(m['orgs'])})")
    for c, n in m["category_counts"].items():
        print(f"    {c:9s} {n}")
    print(f"    研报 {m['reports_searched']} 篇 (问答兜底 {m['qa_fallback_used']}) "
          f"-> {m['output_path']}")
    if m["contract_errors"] or m["semantic_errors"]:
        print(f"    [警告] 自检未过: {m['contract_errors'] + m['semantic_errors'][:3]}")


if __name__ == "__main__":
    main()
