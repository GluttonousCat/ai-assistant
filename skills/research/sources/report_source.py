# -*- encoding: utf-8 -*-
"""
T04 · 研报观点与主张抽取 (Stage 1 证据层, 任务: docs/research/tasks/T04_研报观点与主张抽取.md)

把研报库中对某公司的**观点**整理为 View 分片 (output/research/<ts_code>/views_reports.json)。
观点永远不是事实 — org+publish_date 是观点的身份证 (三层分离契约见 skills/research/contracts)。

数据通路: 只走 harness.mcp 注册表, 禁直连 SQL:
  search_reports (检索清单) → read_report (单篇: 结构化 analysis 优先, 缺核心观点时
  以单篇问答兜底, 默认上限 6 篇) → get_forecasts (盈利预测时序+分歧度, 补齐表内预测)

View 分类 (category):
  viewpoint  核心观点/要点转述 (analysis.core_view / key_points)
  forecast   盈利预测 (必带期间与数值口径; analysis.forecasts / fin.report_forecast)
  rating     评级/目标价 (analysis.rating / target_price 预测)
  risk       风险提示 (analysis.risks)

冲突观点都保留 (多来源交叉, 冲突本身是信息, 不做仲裁);
org 经 core.helpers.normalize_org_name 归一 (英文投行名→中文通用名);
vid = R:<report_id>.<seq> (同一篇内按 核心观点→要点→评级/目标价→预测→风险 顺序编号)。
主标的判定 (analysis.ts_codes 优先, 标题/绑定标的/公司名兜底): 检索可能经 tags 命中
非主标的报告 (如五粮液报告出现在茅台检索), 其观点加「【主标的名】」前缀标注归属,
防止评级/预测数值被误引到目标公司 — 前缀只加归属, 不改原文口径。

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

# View 分类闭集 (T04 任务定义)
CATEGORIES = ("viewpoint", "forecast", "rating", "risk")

# 盈利预测 metric -> 中文名 (report_forecast.forecast_type / analysis.forecasts.metric)
_METRIC_CN = {
    "revenue": "营业收入",
    "net_profit": "净利润",
    "eps": "每股收益",
    "roe": "净资产收益率",
    "target_price": "目标价",
    "gross_margin": "毛利率",
}

# 单篇问答兜底的提问 (read_report question 参数; 要求逐行带标签, 便于确定性解析)
_QA_QUESTION = (
    "请从这篇研报中提取观点并逐行输出, 每行一项, 格式为「标签: 内容」。"
    "标签只能取: 核心观点/评级/目标价/盈利预测/风险。"
    "其中盈利预测行写成「盈利预测: 期间|指标|数值|单位」(如 2026E|净利润|862|亿元)。"
    "研报未提及的标签不要输出, 不加任何解释, 全部用简体中文。"
)

_VID_RE = re.compile(r"^R:\d+\.\d+$")


# ---------- 纯函数 (离线可测) ----------

def _fmt_num(v: Any) -> str:
    """数值转陈述友好的字符串 (整数值去 .0, 其余原样)"""
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def norm_period(p: str) -> str:
    """预测期间归一: 'NA'→''; '2026E' 在契约 valid_period 不认 E 后缀时降级 '2026'
    (估计口径由 statement 原文保留; 契约后续若扩展 YYYYE 则自动保留 E)。"""
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
    """analysis.forecasts 单条 -> (category, statement, period); 口径不全则 (None,..) 丢弃。
    raw_text 是入库时 LLM 的中文转述 (期间+数值+单位自含), 优先原样保留。"""
    metric = str(fc.get("metric") or "").strip()
    period = norm_period(str(fc.get("period") or "").strip())
    value, unit = fc.get("value"), str(fc.get("unit") or "").strip()
    raw = str(fc.get("raw_text") or "").strip()
    is_tp = metric == "target_price"
    if not is_tp and not period and raw:
        pm = re.search(r"20\d{2}", raw)     # 期间缺失时从转述原文抽年份兜底
        period = norm_period(pm.group(0)) if pm else ""
    if not is_tp and not period:
        return (None, "", "")               # forecast 必带期间, 缺则丢弃
    if raw:
        return ("rating" if is_tp else "forecast", raw, "" if is_tp else period)
    if value is None or not unit:
        return (None, "", "")   # 数值口径不完整, 宁缺毋滥
    stmt = (f"目标价 {_fmt_num(value)}{unit}" if is_tp
            else f"预计 {period} {_METRIC_CN.get(metric, metric)}{_fmt_num(value)}{unit}")
    return ("rating" if is_tp else "forecast", stmt, "" if is_tp else period)


def _context_prefix(detail: Optional[Dict[str, Any]], brief: Dict[str, Any],
                    ts_code: str, name: str) -> str:
    """主标的判定: 研报可能经 tags 命中检索而非主标的 (如五粮液报告出现在茅台检索里,
    其评级/预测数值属于五粮液)。主标的报告返回 ''; 非主标的返回语句前缀
    「【主标的名】」— 标注归属防误引, 不改原文口径。判定链:
    analysis.ts_codes 含目标代码 → 主标的; 否则 标题/绑定标的/公司名含目标名 → 主标的;
    均无法判定 → 「【关联提及】」。"""
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
    """read_report 返回体 (或仅 search 清单 brief) -> 该篇的 View 列表 (vid 留空, 统一分配)。

    顺序: 核心观点 → 要点(截断) → 评级 → 预测(目标价归 rating) → 风险(截断);
    篇内 (category, statement) 重复丢弃。org 一律归一;
    context_prefix 为非主标的报告的归属前缀 (见 _context_prefix)。"""
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
            continue    # 观点缺机构/日期即失身份证, 不产出
        key = (cat, stmt)
        if key in seen:
            continue
        seen.add(key)
        views.append(View(vid="", statement=context_prefix + stmt, org=org,
                          publish_date=date, report_id=rid, layer="view",
                          ts_code=ts_code, period=period, category=cat))
    return views


def build_forecast_view(row: Dict[str, Any], ts_code: str = "") -> Optional[View]:
    """get_forecasts.recent_forecasts 单行 (report_forecast 表) -> forecast/rating View"""
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
        return None                         # forecast 必带期间
    if ftype == "target_price":
        return View(vid="", statement=f"目标价 {_fmt_num(value)}{unit}", org=org,
                    publish_date=date, report_id=rid, layer="view", ts_code=ts_code,
                    period="", category="rating")
    return View(vid="", statement=f"预计 {period} {metric}{_fmt_num(value)}{unit}",
                org=org, publish_date=date, report_id=rid, layer="view",
                ts_code=ts_code, period=period, category="forecast")


# 问答兜底输出的行标签 -> 分类 (长标签在前防误配)
_QA_LABELS = [("核心观点", "viewpoint"), ("盈利预测", "forecast"), ("目标价", "rating"),
              ("评级", "rating"), ("风险", "risk"), ("观点", "viewpoint"),
              ("预测", "forecast")]
_QA_LINE_RE = re.compile(
    r"^(核心观点|盈利预测|目标价|评级|风险|观点|预测)\s*[:：]\s*(.+)$")


def parse_qa_views(answer: str, report_id: int, org: str, publish_date: str,
                   ts_code: str = "", max_views: int = 6,
                   context_prefix: str = "") -> List[View]:
    """单篇问答的逐行回答 -> View 列表 (宽容解析: 剥 bullets/序号, 未提及跳过)"""
    org = normalize_org_name(str(org or ""))
    views: List[View] = []
    if not org or not str(publish_date or "").strip():
        return views                        # 观点缺机构/日期即失身份证, 不产出
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
        if label == "目标价":             # 保持单句自含, 脱离上下文仍可读
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
    """按列表顺序分配 vid = R:<report_id>.<seq> (同一篇内从 1 递增, 跨篇独立)"""
    seq: Dict[int, int] = {}
    for v in views:
        rid = int(v.report_id or 0)
        seq[rid] = seq.get(rid, 0) + 1
        v.vid = f"R:{rid}.{seq[rid]}"
    return views


def validate_views(views: List[View]) -> List[str]:
    """T04 语义自检 (T01 validate_shard 之外的分类/口径规则), 返回问题清单 (空=通过):
    category 闭集; forecast 必带 period 且 statement 含数字; vid 格式与唯一;
    statement/org 非空。"""
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


# ---------- 编排 (走 MCP 注册表) ----------

def _mcp():
    from harness.mcp import get_registry
    return get_registry()


def _call(reg, name: str, **kwargs) -> Optional[Dict[str, Any]]:
    """MCP 统一调用; 失败记日志返回 None (单篇失败不拖垮整批)"""
    env = reg.call(name, {k: v for k, v in kwargs.items() if v is not None})
    if not env.get("ok"):
        log.warning("MCP %s(%s) 失败: %s", name, kwargs, env.get("error"))
        return None
    return env.get("data")


def _dup_forecast(views: List[View], rid: int, period: str, metric_cn: str) -> bool:
    """同篇同期间同指标的预测是否已存在 (statement 内含指标词判重, 兼容 raw_text 转述)"""
    return any(v.category == "forecast" and v.report_id == rid
               and v.period == period and metric_cn in v.statement for v in views)


def collect_report_views(stock: str, days: int = 365, out_dir: Path | None = None,
                         limit: int = 30, qa_fallback: int = 6) -> EvidenceShard:
    """收集某股票近 N 天研报观点 -> View 分片并落盘 views_reports.json。

    stock: 股票名或代码; days: 回看天数; out_dir: 输出目录
    (默认 output/research/<ts_code>/); limit: 检索篇数上限 (MCP 上限 30);
    qa_fallback: 结构化 analysis 缺核心观点时单篇问答兜底的篇数上限 (0=关闭)。
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

    # report_forecast 表补齐: 同篇同期间同指标未覆盖才补 (含 search 窗口外的旧预测)
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

    # 收尾: 篇内口径去重 -> 补 ts_code -> 分配 vid -> 契约/语义双校验
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
    """为契约校验器组装临时分片 (collect 内部用, 避免先建 shard 再改 views)"""
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
