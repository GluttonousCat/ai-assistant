# -*- encoding: utf-8 -*-
"""
Listed-company profile: structured data assembly over eight sections (pure
data; no LLM in the assembly layer)

Everything goes through REGISTRY.call to reuse the MCP tool layer (resolve_stock /
query_financials / query_main_business / valuation_percentile / get_forecasts /
verify_forecasts / detect_financial_risk / search_reports / run_quant_scan) —
zero duplicated capability implementations; a single failed section does not
take down the whole profile (recorded in failed_sections).

The output feeds three consumers:
- Chat: returned directly by the build_company_profile tool
- WeChat article: content/article.py (profile -> LLM writes the article)
- PPT: content/ppt.py (profile -> pptgen task book -> slides)
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List

from core.logger import get_logger

logger = get_logger(__name__)

# (section name, tool, args) — order is the narrative order
_SECTIONS: List[tuple] = [
    ("basic", "resolve_stock", {"text": "{stock}"}),
    ("main_business", "query_main_business", {"stock": "{stock}", "top_n": 5}),
    ("financials", "query_financials", {
        "mode": "single", "stocks": ["{stock}"],
        "metrics": ["营收", "净利润", "毛利率", "净资产收益率"],
        "time": "最近{years}年"}),
    ("valuation", "valuation_percentile", {"stock": "{stock}", "years": 3}),
    ("forecasts", "get_forecasts", {"stock": "{stock}"}),
    ("verification", "verify_forecasts", {"stock": "{stock}"}),
    ("risk", "detect_financial_risk", {"stock": "{stock}"}),
    ("reports", "search_reports", {"stock": "{stock}", "days": 180, "limit": 5}),
    ("regime", "run_quant_scan", {"scan_type": "regime", "stock": "{stock}"}),
]


def _fmt_args(args: Dict[str, Any], stock: str, years: int) -> Dict[str, Any]:
    out = {}
    for k, v in args.items():
        if isinstance(v, str):
            v = v.format(stock=stock, years=years)
        elif isinstance(v, list):
            v = [x.format(stock=stock, years=years) if isinstance(x, str) else x
                 for x in v]
        out[k] = v
    return out


def build_company_profile(stock: str, years: int = 5) -> Dict[str, Any]:
    """Assemble a listed-company profile. Returns {stock, generated_at,
    sections, ok_sections, failed_sections}"""
    from harness.mcp import get_registry
    registry = get_registry()
    stock = stock.strip()
    sections: Dict[str, Any] = {}
    ok, failed = [], []

    for name, tool, args in _SECTIONS:
        env = registry.call(tool, _fmt_args(args, stock, years))
        if env.get("ok"):
            sections[name] = env["data"]
            ok.append(name)
        else:
            # degrade on a single failed section (e.g. verify with no
            # historical forecasts); the profile continues
            sections[name] = {"error": env.get("error", "")}
            failed.append(name)
            logger.warning(f"画像板块 {name} 失败: {env.get('error', '')[:80]}")

    basic = (sections.get("basic") or {}).get("stocks") or [{}]
    ts_code = basic[0].get("ts_code")

    # Industry position (needs the industry name from basic, hence appended
    # after the loop): Shenwan L2 ROE peer comparison
    ind_name = basic[0].get("industry_l2") or basic[0].get("industry_l1")
    if ind_name:
        env = registry.call("query_financials", {
            "mode": "industry", "industry": ind_name,
            "metrics": ["净资产收益率"], "top_n": 12})
        if env.get("ok"):
            sections["industry"] = env["data"]
            ok.append("industry")

    # Top-10 holders (latest period + holding-change signals)
    if ts_code:
        holders = _load_holders(ts_code)
        if holders:
            sections["holders"] = holders
            ok.append("holders")

    return {
        "stock": stock,
        "ts_code": ts_code,
        "name": basic[0].get("name", stock),
        "years": years,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "sections": sections,
        "ok_sections": ok,
        "failed_sections": failed,
    }


def _load_holders(ts_code: str) -> Dict[str, Any]:
    """Top-10 holders of the latest report period + change signals (holding
    ratio/change; source stock.top10_holders)"""
    from storage.pg import PgClient
    try:
        with PgClient() as pg:
            latest = pg.fetch_one(
                "SELECT MAX(end_date) d FROM stock.top10_holders WHERE ts_code=%s",
                (ts_code,))
            if not latest or not latest["d"]:
                return {}
            rows = pg.fetch_all(
                "SELECT holder_name, hold_ratio, hold_float_ratio, "
                "       hold_change, holder_type, end_date "
                "FROM stock.top10_holders WHERE ts_code=%s AND end_date=%s "
                "ORDER BY hold_ratio DESC NULLS LAST LIMIT 10",
                (ts_code, latest["d"]))
            return {"end_date": str(latest["d"]),
                    "holders": [dict(r) for r in rows]}
    except Exception as e:  # noqa: BLE001 the table may not be synced yet
        logger.debug(f"十大股东读取跳过: {e}")
        return {}


def _yi(v) -> str:
    """Yuan -> hundred-million (亿) string (None stays —)"""
    if v is None:
        return "—"
    try:
        return f"{float(v) / 1e8:.1f}"
    except (TypeError, ValueError):
        return str(v)


def profile_digest(profile: Dict[str, Any], max_chars: int = 6000) -> str:
    """Profile -> digest for article writing (token control: keep key numbers, drop wide tables)"""
    import json
    s = profile.get("sections", {})
    parts = [f"公司: {profile.get('name')} ({profile.get('ts_code')})",
             f"画像时间: {profile.get('generated_at')} "
             f"(板块完整: {profile.get('ok_sections')})"]

    b = s.get("basic") or {}
    if b.get("stocks"):
        st = b["stocks"][0]
        parts.append(f"[概况] 申万行业: {st.get('industry_l1')}/{st.get('industry_l2')}, "
                     f"上市: {st.get('list_date')}")

    mb = s.get("main_business") or {}
    if mb.get("data"):
        # prefer the product dimension (P) to avoid duplicate calibers from
        # the region/industry dimensions
        prod = [r for r in mb["data"] if r.get("bz_code") == "P"] or mb["data"]
        items = [f"{r.get('bz_item')} 占比{r.get('sales_share_pct')}%"
                 f"(毛利率{r.get('gross_margin_pct')}%)"
                 for r in prod[:4] if r.get("bz_item")]
        parts.append("[主营构成] " + "; ".join(items))

    fin = s.get("financials") or {}
    if fin.get("data"):
        # column names are the original SQL fields; aggregate by year: prefer
        # the annual row (12-31), else the latest period of that year
        by_year: Dict[str, Dict] = {}
        for r in fin["data"]:
            y = str(r.get("end_date", ""))[:4]
            if not y:
                continue
            if (y not in by_year
                    or str(r.get("end_date", "")).endswith("12-31")):
                by_year[y] = r
        rows = []
        for y in sorted(by_year):
            r = by_year[y]
            rows.append(f"{y}: 营收 {_yi(r.get('revenue'))} | "
                        f"归母净利 {_yi(r.get('n_income_attr_p'))} | "
                        f"毛利率 {r.get('grossprofit_margin')}% | "
                        f"ROE {r.get('roe')}%")
        if rows:
            parts.append("[财务年度·亿]\n  " + "\n  ".join(rows))

    val = s.get("valuation") or {}
    pe, pb = val.get("pe_ttm"), val.get("pb")
    if pe or pb:
        parts.append(f"[估值分位(3年)] PE: {json.dumps(pe, ensure_ascii=False)}, "
                     f"PB: {json.dumps(pb, ensure_ascii=False)}")

    fc = s.get("forecasts") or {}
    if fc.get("divergence_stats"):
        parts.append("[盈利预测分歧] " + json.dumps(
            fc["divergence_stats"][:3], ensure_ascii=False))

    vf = s.get("verification") or {}
    if vf.get("verified_groups"):
        parts.append(f"[预测兑现] {vf.get('hit_rate_10pct')}% 命中(±10%), "
                     f"平均偏差 {vf.get('mean_abs_err_pct')}%, {vf.get('bias')}")

    rk = s.get("risk") or {}
    if rk.get("alerts"):
        parts.append("[风险信号] " + "; ".join(
            f"{a['rule']}({a['level']}): {a['message']}" for a in rk["alerts"]))

    rp = s.get("reports") or {}
    if rp.get("reports"):
        parts.append("[近期研报] " + "; ".join(
            f"{r.get('org')}《{str(r.get('title'))[:24]}》{r.get('rating') or ''}"
            for r in rp["reports"][:4]))

    ind = s.get("industry") or {}
    if ind.get("data"):
        rank = [(i + 1, r["stock_name"], r["value"])
                for i, r in enumerate(ind["data"])]
        mine = [(i, n, v) for i, n, v in rank if n == profile.get("name")]
        if mine:
            parts.append(f"[行业坐标] 申万同业 ROE 排名: 第 {mine[0][0]}/"
                         f"{len(rank)} (ROE {mine[0][2]}%); "
                         f"同业前列: " + ", ".join(
                             f"{n} {v}%" for _, n, v in rank[:3]))

    hd = s.get("holders") or {}
    if hd.get("holders"):
        items = []
        for h in hd["holders"][:5]:
            chg = h.get("hold_change")
            mark = ""
            if chg is not None and float(chg or 0) != 0:
                mark = f" (变动 {float(chg)/1e4:+.0f}万)"
            items.append(f"{(h['holder_name'] or '')[:14]} "
                         f"{h.get('hold_ratio')}%{mark}")
        parts.append(f"[十大股东·{hd.get('end_date', '')}] " + "; ".join(items))

    # Note: the quant regime section is deliberately excluded from the article
    # digest — articles contain no quant content

    text = "\n".join(parts)
    return text[:max_chars] + ("\n…(截断)" if len(text) > max_chars else "")


if __name__ == "__main__":
    import sys
    import json
    p = build_company_profile(sys.argv[1] if len(sys.argv) > 1 else "中际旭创")
    print(json.dumps({k: p[k] for k in
                      ("name", "ts_code", "ok_sections", "failed_sections")},
                     ensure_ascii=False))
    print("\n---- digest ----\n", profile_digest(p))
