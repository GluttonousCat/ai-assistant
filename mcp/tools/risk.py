# -*- encoding: utf-8 -*-
"""
风控/校验域工具 (2个, 全新实现):
- detect_financial_risk   财务排雷 (多规则扫描近8报告期, 输出风险信号清单)
- verify_forecasts        券商预测兑现校验 (历史预测 vs 实际值, 命中率/偏差)

数据底座: fin.fina_indicator / income / balancesheet / cashflow + report_forecast,
全部存量数据, 无 LLM。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcp.registry import REGISTRY
from mcp.spec import ToolError, obj_schema, param
from mcp.tools._common import _jsonable, resolve_one


def _num(v) -> Optional[float]:
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


# ============================================================
# 17. detect_financial_risk
# ============================================================

@REGISTRY.tool(
    name="detect_financial_risk",
    domain="risk",
    description=(
        "财务排雷: 对个股近 8 个报告期跑多规则风险扫描, 输出风险信号清单与等级。"
        "规则含: 毛利率连续下滑、营收利润增速背离、净利润增速骤降、经营现金流与利润背离、"
        "商誉占净资产比过高、杠杆抬升、应收增速远超营收、扣非利润占比低。"
        "用户问 'XX有没有财务风险'/'XX财务健康吗'/'帮我排雷' 时调用。"
        "规则信号仅供筛查参考, 不构成审计结论。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
    }, ["stock"]),
    examples=["中际旭创有没有财务风险", "帮我看看某公司的财务健康度", "宁德时代排雷"],
)
def detect_financial_risk(stock: str) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        ind = pg.fetch_all(
            "SELECT end_date, or_yoy, netprofit_yoy, grossprofit_margin, "
            "       debt_to_assets, profit_dedt, ocf_yoy "
            "FROM fin.fina_indicator "
            "WHERE ts_code=%s AND report_type='1' "
            "ORDER BY end_date DESC LIMIT 8", (hit["ts_code"],))
        fin_rows = pg.fetch_all(
            "SELECT i.end_date, i.revenue, i.n_income_attr_p, "
            "       i.net_after_nr_lp_correct, c.n_cashflow_act, "
            "       b.goodwill, b.equity_attr_p, b.accounts_receiv, "
            "       b.total_assets "
            "FROM fin.income i "
            "LEFT JOIN fin.cashflow c ON i.ts_code=c.ts_code "
            "     AND i.end_date=c.end_date AND i.report_type=c.report_type "
            "LEFT JOIN fin.balancesheet b ON i.ts_code=b.ts_code "
            "     AND i.end_date=b.end_date AND i.report_type=b.report_type "
            "WHERE i.ts_code=%s AND i.report_type='1' "
            "ORDER BY i.end_date DESC LIMIT 8", (hit["ts_code"],))
    if not ind and not fin_rows:
        raise ToolError(f"{hit['name']} 无财务数据")

    alerts: List[Dict[str, Any]] = []

    def add(rule: str, level: str, msg: str, values: Dict[str, Any]):
        alerts.append({"rule": rule, "level": level, "message": msg,
                       "values": {k: _jsonable(v) for k, v in values.items()}})

    # ---- 指标序列 (升序: 旧->新) ----
    ind_asc = list(reversed(ind))
    gms = [(_str_date(r["end_date"]), _num(r["grossprofit_margin"])) for r in ind_asc]
    gm_vals = [v for _, v in gms if v is not None]

    # 1. 毛利率连续下滑 (近3期严格递减, 累计降幅>3pp)
    if len(gm_vals) >= 3 and all(
            gm_vals[-i - 1] < gm_vals[-i - 2] for i in range(3)):
        drop = gm_vals[-1 - 2] - gm_vals[-1]
        if drop > 3:
            add("毛利率连续下滑", "medium" if drop < 8 else "high",
                f"毛利率连续3期下滑, 累计降 {drop:.1f}pp",
                {"series": gm_vals[-4:], "latest_end_date": gms[-1][0]})

    # 2. 营收利润背离 (营收增长但利润增速大幅落后)
    if ind:
        r = ind[0]
        or_yoy, np_yoy = _num(r["or_yoy"]), _num(r["netprofit_yoy"])
        if or_yoy is not None and np_yoy is not None and or_yoy > 5 \
                and np_yoy < or_yoy - 15:
            add("营收利润背离", "medium",
                f"营收同比 +{or_yoy:.1f}% 但净利同比 {np_yoy:.1f}%, 增速差 "
                f"{or_yoy - np_yoy:.0f}pp",
                {"or_yoy": or_yoy, "netprofit_yoy": np_yoy,
                 "end_date": _str_date(r["end_date"])})

    # 3. 净利润增速骤降 (相邻报告期落差>25pp)
    if len(ind) >= 2:
        cur, prev = _num(ind[0]["netprofit_yoy"]), _num(ind[1]["netprofit_yoy"])
        if cur is not None and prev is not None and prev - cur > 25:
            add("净利润增速骤降", "medium",
                f"净利同比从 {prev:.1f}% 骤降到 {cur:.1f}%",
                {"prev": prev, "latest": cur,
                 "end_date": _str_date(ind[0]["end_date"])})

    # 4. 现金流与利润背离 (净利润>0 但经营现金流/净利<0.5, <0 更严重)
    if fin_rows:
        r = fin_rows[0]
        np_v, ocf = _num(r["n_income_attr_p"]), _num(r["n_cashflow_act"])
        if np_v is not None and np_v > 0 and ocf is not None:
            ratio = ocf / np_v
            if ratio < 0:
                add("经营现金流为负", "high",
                    f"净利润 {np_v/1e8:.1f}亿 但经营现金流净额为负 ({ocf/1e8:.1f}亿)",
                    {"net_profit_yi": np_v / 1e8, "ocf_yi": ocf / 1e8,
                     "ratio": round(ratio, 2),
                     "end_date": _str_date(r["end_date"])})
            elif ratio < 0.5:
                add("现金流与利润背离", "medium",
                    f"经营现金流仅为净利润的 {ratio*100:.0f}%",
                    {"net_profit_yi": np_v / 1e8, "ocf_yi": ocf / 1e8,
                     "ratio": round(ratio, 2),
                     "end_date": _str_date(r["end_date"])})

    # 5. 商誉占净资产比
    if fin_rows:
        r = fin_rows[0]
        gw, eq = _num(r["goodwill"]), _num(r["equity_attr_p"])
        if gw is not None and eq and eq > 0:
            share = gw / eq * 100
            if share > 20:
                add("商誉占比过高", "high" if share > 40 else "medium",
                    f"商誉 {gw/1e8:.1f}亿 占归母净资产 {share:.0f}%",
                    {"goodwill_yi": gw / 1e8, "equity_yi": eq / 1e8,
                     "share_pct": round(share, 1),
                     "end_date": _str_date(r["end_date"])})

    # 6. 杠杆抬升 (资产负债率较8期前+10pp 且 >60%)
    dts = [_num(r["debt_to_assets"]) for r in ind_asc]
    dts = [v for v in dts if v is not None]
    if len(dts) >= 4:
        if dts[-1] > 60 and dts[-1] - dts[0] > 10:
            add("杠杆持续抬升", "medium",
                f"资产负债率从 {dts[0]:.0f}% 升到 {dts[-1]:.0f}%",
                {"debt_to_assets_series": dts})

    # 7. 应收增速远超营收 (年报口径同比)
    if len(fin_rows) >= 5:
        def _annual(rows, key):
            out = {}
            for r in rows:
                d = _str_date(r["end_date"])
                if d.endswith("12-31") and _num(r.get(key)) is not None:
                    out[d[:4]] = _num(r[key])
            return out
        ar_map, rev_map = _annual(fin_rows, "accounts_receiv"), _annual(fin_rows, "revenue")
        years = sorted(set(ar_map) & set(rev_map))
        if len(years) >= 2:
            y0, y1 = years[-2], years[-1]
            ar_g = (ar_map[y1] / ar_map[y0] - 1) * 100 if ar_map[y0] else None
            rev_g = (rev_map[y1] / rev_map[y0] - 1) * 100 if rev_map[y0] else None
            if ar_g is not None and rev_g is not None and ar_g > rev_g + 20:
                add("应收增速远超营收", "medium",
                    f"应收账款同比 +{ar_g:.0f}% 远超营收同比 +{rev_g:.0f}% ({y1}年报)",
                    {"ar_yoy": round(ar_g, 1), "revenue_yoy": round(rev_g, 1),
                     "year": y1})

    # 8. 扣非利润占比低 (扣非/归母 < 50%)
    if fin_rows:
        r = fin_rows[0]
        dedt, np_v = _num(r.get("net_after_nr_lp_correct")), _num(r["n_income_attr_p"])
        if dedt is not None and np_v and np_v > 0 and dedt / np_v < 0.5:
            add("扣非利润占比低", "info",
                f"扣非净利润仅为归母净利润的 {dedt/np_v*100:.0f}% (非经常损益占比高)",
                {"deducted_ratio": round(dedt / np_v, 2),
                 "end_date": _str_date(r["end_date"])})

    level_order = {"high": 3, "medium": 2, "info": 1}
    overall = "clean" if not alerts else max(
        (a["level"] for a in alerts), key=lambda l: level_order[l])
    return {
        "stock": hit,
        "periods_scanned": len(ind) or len(fin_rows),
        "latest_end_date": _str_date(ind[0]["end_date"]) if ind else None,
        "risk_level": overall,
        "alert_count": len(alerts),
        "alerts": alerts,
        "disclaimer": "规则筛查结果, 非审计结论; 建议结合年报附注与研报交叉验证",
    }


def _str_date(d) -> str:
    return d.isoformat() if hasattr(d, "isoformat") else str(d)


# ============================================================
# 18. verify_forecasts
# ============================================================

# 预测指标 -> 实际值取数 (A股口径)
_FORECAST_ACTUAL = {
    "revenue": ("fin.income", "revenue", "元"),
    "net_profit": ("fin.income", "n_income_attr_p", "元"),
    "eps": ("fin.income", "basic_eps", "元/股"),
    "roe": ("fin.fina_indicator", "roe", "%"),
    "gross_margin": ("fin.fina_indicator", "grossprofit_margin", "%"),
}

_CNY_MAGNITUDE = [
    ("亿元", 1e8), ("亿", 1e8), ("十亿", 1e10), ("百万元", 1e6), ("百万", 1e6),
    ("万", 1e4), ("元", 1.0), ("percent", 1.0), ("%", 1.0),
]


def _cny_factor(unit: str) -> Optional[float]:
    """人民币单位 -> 元 的倍率 (/%类单位原值); 非人民币或未知单位返回 None"""
    u = (unit or "").strip().replace("人民币", "").replace("RMB", "")
    if any(mark in u for mark in ("美元", "美金", "日元", "台币", "港元",
                                  "韩元", "NT$", "$", "¥", "US")):
        return None
    if not u:
        return 1.0
    for k, v in _CNY_MAGNITUDE:      # 长词优先, 兼容 '亿元/年' 等尾缀
        if u.startswith(k) or u == k:
            return v
    return None


@REGISTRY.tool(
    name="verify_forecasts",
    domain="risk",
    description=(
        "券商预测兑现校验: 把历史上券商对某股的盈利预测 (营收/净利润/EPS/ROE/毛利率) "
        "与后来披露的实际值对比, 输出各期偏差与总体命中率/偏向。"
        "用户问 '券商预测准不准'/'XX的预期兑现了吗'/'研报可信度' 时调用。"
        "只校验已出年报的过去期间与人民币口径预测; 未来期间的预测请用 get_forecasts。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
    }, ["stock"]),
    examples=["券商对贵州茅台的预测准不准", "中际旭创的预期兑现了吗", "研报预测的历史可信度"],
)
def verify_forecasts(stock: str) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT rf.forecast_type, rf.forecast_period, rf.forecast_value, "
            "       rf.forecast_unit, rm.org_name, rm.publish_date "
            "FROM fin.report_forecast rf "
            "LEFT JOIN fin.report_meta rm ON rm.report_id = rf.report_id "
            "WHERE rf.ts_code=%s AND rf.forecast_value IS NOT NULL "
            "ORDER BY rf.forecast_period", (hit["ts_code"],))
    if not rows:
        raise ToolError(f"{hit['name']} 无盈利预测数据")

    import re
    from collections import defaultdict
    groups = defaultdict(list)   # (type, year) -> [(value_yuan_or_native, org, date)]
    skipped = defaultdict(int)   # 原因 -> 次数
    for r in rows:
        ftype = (r["forecast_type"] or "").strip()
        period = str(r["forecast_period"] or "").strip()
        if ftype not in _FORECAST_ACTUAL:
            skipped[f"不支持的指标: {ftype}"] += 1
            continue
        m = re.match(r"^(20\d{2})", period)
        if not m:
            skipped["期间无法解析"] += 1
            continue
        year = int(m.group(1))
        val = _num(r["forecast_value"])
        unit = (r["forecast_unit"] or "").strip()
        factor = _cny_factor(unit)
        if val is None or factor is None:
            skipped[f"币种/单位跳过: {unit or '空'}"] += 1
            continue
        groups[(ftype, year)].append((val * factor, r.get("org_name"),
                                      _str_date(r.get("publish_date") or "")))

    details, n_hit, n_total = [], 0, 0
    signed_errs: List[float] = []
    with PgClient() as pg:
        for (ftype, year), items in sorted(groups.items(),
                                           key=lambda kv: (kv[0][1], kv[0][0])):
            table, col, unit_desc = _FORECAST_ACTUAL[ftype]
            row = pg.fetch_one(
                f"SELECT {col} AS v FROM {table} "
                f"WHERE ts_code=%s AND end_date=%s "
                f"AND report_type='1' LIMIT 1",
                (hit["ts_code"], f"{year}-12-31"))
            if not row or row["v"] is None:
                continue  # 年报未出, 无法验证
            actual = float(row["v"])
            vals = [v for v, _, _ in items]
            mean_f = sum(vals) / len(vals)
            err_pct = (actual - mean_f) / abs(mean_f) * 100 if mean_f else None
            n_total += 1
            hit_ok = err_pct is not None and abs(err_pct) <= 10
            n_hit += int(hit_ok)
            if err_pct is not None:
                signed_errs.append(err_pct)
            details.append({
                "forecast_type": ftype, "year": year,
                "n_forecasts": len(items),
                "n_orgs": len({o for _, o, _ in items if o}),
                "mean_forecast": round(mean_f, 4), "actual": round(actual, 4),
                "unit": unit_desc,
                "err_pct": round(err_pct, 1) if err_pct is not None else None,
                "verdict": ("兑现" if hit_ok else
                            "实际不及预期" if (err_pct or 0) < 0 else "实际超预期"),
            })

    if not signed_errs:
        bias = "无足够样本"
    elif sum(signed_errs) / len(signed_errs) < -3:
        bias = "实际整体不及券商预期 (预期偏乐观)"
    elif sum(signed_errs) / len(signed_errs) > 3:
        bias = "实际整体好于券商预期 (预期偏保守)"
    else:
        bias = "实际与预期基本一致"
    return {
        "stock": hit,
        "verified_groups": n_total,
        "hit_rate_10pct": round(n_hit / n_total * 100, 1) if n_total else None,
        "mean_abs_err_pct": round(sum(abs(e) for e in signed_errs) / len(signed_errs),
                                  1) if signed_errs else None,
        "bias": bias,
        "details": details,
        "skipped": dict(skipped),
        "note": "只校验人民币口径 + 已披露年报的期间; 未来预测见 get_forecasts",
    }
