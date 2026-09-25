# -*- encoding: utf-8 -*-
"""
T02 · Tushare/画像事实源 (研究生产线 Stage 1, 事实层)

把画像八板块 + 关键财务时序转换为 Fact 证据分片 facts_tushare.json。
数据获取全部走现有能力 (禁直写 SQL):
  - skills/content/profile.py build_company_profile() — 八板块画像 (只 import 不修改)
  - harness.mcp 注册表 query_financials — 补齐财务三表时序
    (single 模式单次最多 3 个指标, 且画像 financials 板块本身只带营收/净利/毛利率
     3 列, 故按 3 指标一批补查 12 项)

三层分离: 本分片只产 fact 层。画像中的 forecasts/verification/reports/risk/regime
板块属观点/判断层 (T04/T09 职责), 刻意不纳入。

口径约定:
  - 财务行取年报行 (12-31) 优先 → 年报=全年累计, 免环比换算; 某年只有季报行时
    按实际期间标注 (YYYYQ1/Q3 累计口径), 不跨期换算。
  - 金额字段 (元) ÷1e8 折算亿元表述, confidence=0.9 并在 source 注明公式;
    比率/每股类直引原值, confidence=1.0。

CLI: python -m skills.research.sources.tushare_source 贵州茅台 [--years 5] [--out DIR]
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from core.logger import get_logger
from skills.research.contracts import (
    EvidenceShard,
    Fact,
    research_dir,
    save_shard,
)

logger = get_logger(__name__)


# ---------- 指标元数据 (slug 唯一; bare=SQL 裸列名; field=回溯用 表.字段) ----------

def _m(slug: str, cn: str, unit: str, field: str, yuan: bool = False,
       query: str = "") -> Dict[str, Any]:
    """cn=Fact 显示名; query=query_financials 别名表精确键 (缺省同 cn)"""
    return {"slug": slug, "cn": cn, "unit": unit, "field": field,
            "bare": field.split(".", 2)[-1], "yuan": yuan,
            "query": query or cn}


# 画像 financials 板块自带的 3 列 (build_company_profile 内部已查)
_BASE_METRICS = [
    _m("revenue", "营业收入", "亿元", "fin.income.revenue", yuan=True),
    _m("n_income", "归母净利润", "亿元", "fin.income.n_income_attr_p", yuan=True),
    _m("gross_margin", "销售毛利率", "%", "fin.fina_indicator.grossprofit_margin"),
]

# 补查时序批次 (每批 ≤3 指标, 覆盖三表 + 盈利质量 + 成长性);
# query 必须是 METRIC_ALIAS_FULL 的精确键, 否则整批被 _resolve_metric 拒绝
_EXTRA_BATCHES = [
    [_m("roe", "净资产收益率ROE", "%", "fin.fina_indicator.roe",
         query="净资产收益率"),
     _m("net_margin", "销售净利率", "%", "fin.fina_indicator.netprofit_margin",
         query="净利率"),
     _m("debt_ratio", "资产负债率", "%", "fin.fina_indicator.debt_to_assets")],
    [_m("ocf", "经营活动现金流净额", "亿元", "fin.cashflow.n_cashflow_act",
         yuan=True, query="经营现金流"),
     _m("total_assets", "总资产", "亿元", "fin.balancesheet.total_assets", yuan=True),
     _m("net_assets", "归母净资产", "亿元", "fin.balancesheet.equity_attr_p",
         yuan=True, query="净资产")],
    [_m("eps", "基本每股收益", "元", "fin.income.basic_eps",
         query="每股收益"),
     _m("or_yoy", "营业收入同比增长", "%", "fin.fina_indicator.or_yoy",
         query="营收同比增长"),
     _m("np_yoy", "归母净利润同比增长", "%", "fin.fina_indicator.netprofit_yoy",
         query="净利润同比增长")],
    [_m("deduct_n_income", "扣非归母净利润", "亿元", "fin.income.net_after_nr_lp_correct",
         yuan=True, query="扣非净利润"),
     _m("operate_profit", "营业利润", "亿元", "fin.income.operate_profit", yuan=True),
     _m("inventory", "存货", "亿元", "fin.balancesheet.invent", yuan=True)],
]
_EXTRA_BY_SLUG = {m["slug"]: m for batch in _EXTRA_BATCHES for m in batch}

_BIZ_CN = {"P": "产品", "D": "地区", "I": "行业"}


# ---------- 期间口径 ----------

def _period_label(end_date: str) -> str:
    """end_date(YYYY-MM-DD) → 期间标签: 12-31→YYYYA, 06-30→YYYYH1, 03-31→YYYYQ1, 09-30→YYYYQ3"""
    y, md = end_date[:4], end_date[5:10]
    return {"12-31": f"{y}A", "06-30": f"{y}H1",
            "03-31": f"{y}Q1", "09-30": f"{y}Q3"}.get(md, end_date)


def _period_cn(label: str) -> str:
    """'2024A' → '2024年报' (Q1/Q3/H1 均为累计口径)"""
    if len(label) >= 5 and label[:4].isdigit():
        suf = label[4:]
        cn = {"A": "年报", "H1": "中报", "Q1": "一季报", "Q3": "三季报"}.get(suf)
        if cn:
            return label[:4] + cn
    return label


def _fnum(v: Any) -> Optional[float]:
    """宽容数值化 (None/NaN/空串 → None; Decimal→float)"""
    if v is None or v == "":
        return None
    try:
        f = float(v)
        return None if f != f else f  # NaN 检查
    except (TypeError, ValueError):
        return None


def _annual_rows(rows: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """同年多期行 → 年报行(12-31)优先 (财报累计口径, 年报=全年免环比换算)"""
    by_year: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        end_date = str(r.get("end_date") or "")
        y = end_date[:4]
        if not y:
            continue
        if y not in by_year or end_date.endswith("12-31"):
            by_year[y] = r
    return by_year


def _clip_years(by_year: Dict[str, Dict[str, Any]],
                years: int) -> Dict[str, Dict[str, Any]]:
    """截断到最近 years 个年报年度 + 晚于最大年报年的进行中期 (如当年中报)。

    query_financials 时序返回可达 12 年 (LIMIT 48), 年限约束由本层保证。
    """
    annual = {y for y, r in by_year.items()
              if str(r.get("end_date") or "").endswith("12-31")}
    keep_annual = sorted(annual)[-years:] if years > 0 else sorted(annual)
    max_annual = keep_annual[-1] if keep_annual else "0000"
    return {y: r for y, r in by_year.items()
            if y in keep_annual or (y > max_annual and y not in annual)}


# ---------- 各板块 → Fact 列表 (纯转换, 可离线测试) ----------

def _facts_basic(sections: Dict[str, Any], name: str, ts_code: str,
                 snap: str) -> List[Fact]:
    st = ((sections.get("basic") or {}).get("stocks") or [{}])[0]
    out = []
    if st.get("ts_code"):
        out.append(Fact(
            eid=f"T:profile.basic.identity.{snap}",
            content=f"{name} 的证券代码为 {st['ts_code']}"
                    f" (实体解析命中「{st.get('matched', name)}」)",
            source="画像板块basic(工具resolve_stock)|stock.stock_basic.ts_code",
            period=snap))
    if st.get("industry_l1") or st.get("industry_l2"):
        out.append(Fact(
            eid=f"T:profile.basic.industry.{snap}",
            content=f"{name} 申万行业归属: 一级 {st.get('industry_l1', '未分类')}"
                    f" / 二级 {st.get('industry_l2', '未分类')}",
            source="画像板块basic(工具resolve_stock)|stock.stock_basic.industry_l1/l2",
            period=snap))
    if st.get("market"):
        out.append(Fact(
            eid=f"T:profile.basic.market.{snap}",
            content=f"{name} 上市市场: {st['market']}",
            source="画像板块basic(工具resolve_stock)|stock.stock_basic.market",
            period=snap))
    return out


def _facts_mainbiz(sections: Dict[str, Any]) -> List[Fact]:
    mb = sections.get("main_business") or {}
    rows = mb.get("data") or []
    end_date = str(mb.get("end_date") or "")
    if not rows or not end_date:
        return []
    label = _period_label(end_date)
    # 同画像摘要口径: 优先产品维度(P), 避免与地区/行业维度重复计口径
    prod = [r for r in rows if r.get("biz_type") == "P"] or rows
    out = []
    for i, r in enumerate(prod[:5], 1):
        item = r.get("bz_item")
        if not item:
            continue
        sales = _fnum(r.get("bz_sales"))
        share = _fnum(r.get("sales_share_pct"))
        gm = _fnum(r.get("gross_margin_pct"))
        biz_cn = _BIZ_CN.get(str(r.get("biz_type") or ""), str(r.get("biz_type")))
        parts = [f"{item} (主营构成·{biz_cn}维度)"]
        if sales is not None:
            parts.append(f"收入 {sales / 1e8:.2f} 亿元")
        if share is not None:
            parts.append(f"占营业收入 {share:.2f}%")
        if gm is not None:
            parts.append(f"该业务毛利率 {gm:.2f}%")
        out.append(Fact(
            eid=f"T:profile.mainbiz.{label}.{i}",
            content=f"{_period_cn(label)}({end_date}) " + ", ".join(parts),
            source="画像板块main_business(工具query_main_business)"
                   "|fin.v_main_biz.bz_sales|原值单位元,÷1e8折亿元",
            period=label,
            value=(round(sales / 1e8, 4) if sales is not None else None), unit="亿元",
            confidence=0.9))
    return out


def _metric_fact(name: str, m: Dict[str, Any], row: Dict[str, Any],
                 via: str) -> Optional[Fact]:
    """单指标单期一行 → Fact; 值缺失返回 None"""
    end_date = str(row.get("end_date") or "")
    raw = _fnum(row.get(m["bare"]))
    if raw is None or not end_date:
        return None
    label = _period_label(end_date)
    src = f"{via}|{m['field']}"
    if m["yuan"]:
        val, conf = round(raw / 1e8, 4), 0.9
        src += "|原值单位元,÷1e8折亿元"
    else:
        val, conf = round(raw, 4), 1.0
    return Fact(
        eid=f"T:tushare.{m['slug']}.{label}",
        content=f"{name} {_period_cn(label)}({end_date}) {m['cn']} {val:.2f}{m['unit']}",
        source=src, period=label, value=val, unit=m["unit"], confidence=conf)


def _facts_financials(sections: Dict[str, Any],
                      extra: Dict[str, Dict[str, Dict[str, Any]]],
                      name: str, years: int) -> List[Fact]:
    out: List[Fact] = []
    fin = sections.get("financials") or {}
    base_rows = _clip_years(_annual_rows(fin.get("data") or []), years)
    for m in _BASE_METRICS:
        for y in sorted(base_rows):
            f = _metric_fact(name, m, base_rows[y],
                             via="画像板块financials(工具query_financials)")
            if f:
                out.append(f)
    for slug, by_year in extra.items():
        m = _EXTRA_BY_SLUG[slug]
        for y in sorted(by_year):
            f = _metric_fact(name, m, by_year[y],
                             via=f"工具query_financials(mode=single,metrics含「{m['cn']}」)")
            if f:
                out.append(f)
    return out


def _facts_valuation(sections: Dict[str, Any], snap: str) -> List[Fact]:
    val = sections.get("valuation") or {}
    years = val.get("years", 3)
    out = []
    for key, cn in (("pe_ttm", "市盈率PE(TTM)"), ("pb", "市净率PB")):
        d = val.get(key)
        if not d or d.get("latest") is None:
            continue
        out.append(Fact(
            eid=f"T:profile.valuation.{key}.{snap}",
            content=f"{cn} 为 {d['latest']} 倍, 处于近 {years} 年 "
                    f"{d.get('pct_rank')}% 分位 (区间 {d.get('min')}~{d.get('max')}, "
                    f"样本 {d.get('n_days')} 个交易日)",
            source="画像板块valuation(工具valuation_percentile)"
                   f"|stock.daily_basic.{key}|分位=区间内低于当前值交易日占比",
            period=snap, value=_fnum(d.get("latest")), unit="倍",
            confidence=0.9))
    return out


def _facts_holders(sections: Dict[str, Any]) -> List[Fact]:
    hd = sections.get("holders") or {}
    end_date = str(hd.get("end_date") or "")
    rows = hd.get("holders") or []
    if not end_date or not rows:
        return []
    label = _period_label(end_date)
    out = []
    for rank, h in enumerate(rows[:5], 1):
        ratio = _fnum(h.get("hold_ratio"))
        if ratio is not None:
            ratio = round(ratio, 4)
        holder = str(h.get("holder_name") or "")
        if ratio is not None:
            content = (f"{_period_cn(label)}({end_date}) 第{rank}大股东 {holder} "
                       f"持股占比 {ratio:.2f}%")
        else:
            content = (f"{_period_cn(label)}({end_date}) 第{rank}大股东 {holder}")
        out.append(Fact(
            eid=f"T:profile.holders.{label}.{rank}",
            content=content,
            source="画像板块holders|stock.top10_holders.hold_ratio",
            period=label, value=ratio, unit="%"))
    return out


def _facts_industry(sections: Dict[str, Any], name: str, ts_code: str,
                    snap: str) -> List[Fact]:
    ind = sections.get("industry") or {}
    rows = ind.get("data") or []
    if not rows:
        return []
    mine = [(i + 1, r) for i, r in enumerate(rows)
            if r.get("stock_name") == name or r.get("ts_code") == ts_code]
    if not mine:
        return []
    rank, r = mine[0]
    roe = _fnum(r.get("value"))
    roe_txt = f"{roe:.2f}%" if roe is not None else "无数据"
    top3 = ", ".join(f"{x.get('stock_name')} {_fnum(x.get('value')):.1f}%"
                     for x in rows[:3] if _fnum(x.get("value")) is not None)
    end_date = str(r.get("end_date") or "")
    label = _period_label(end_date) if end_date else snap
    return [Fact(
        eid=f"T:profile.industry.roe_rank.{label}",
        content=f"申万同业 ROE 对比: {name} 排名第 {rank}/{len(rows)}"
                f" (ROE {roe_txt}); 同业前三: {top3}",
        source="画像板块industry(工具query_financials mode=industry)"
               "|ROE同业排名计算(当日快照)",
        period=label, value=roe, unit="%", confidence=0.9)]


# ---------- 取数 (走现有能力) ----------

def _fetch_extra_series(registry: Any, stock: str,
                        years: int) -> Dict[str, Dict[str, Dict[str, Any]]]:
    """补查财务时序 → {slug: {year: row}}; 单批失败降级继续 (不拖垮分片)"""
    series: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for batch in _EXTRA_BATCHES:
        env = registry.call("query_financials", {
            "mode": "single", "stocks": [stock],
            "metrics": [m["query"] for m in batch],
            "time": f"最近{years}年"})
        if not env.get("ok"):
            logger.warning(f"补查指标批 {[m['cn'] for m in batch]} 失败: "
                           f"{str(env.get('error', ''))[:80]}")
            continue
        rows = ((env.get("data") or {}).get("data")) or []
        by_year = _clip_years(_annual_rows(rows), years)
        for m in batch:
            series[m["slug"]] = by_year
    return series


# ---------- 主入口 ----------

def collect_tushare_facts(stock: str, years: int = 5,
                          out_dir: Optional[Path] = None,
                          profile: Optional[Dict[str, Any]] = None,
                          registry: Any = None) -> EvidenceShard:
    """股票名/代码 → 画像八板块 + 财务时序 → EvidenceShard(kind="tushare")。

    profile/registry 可注入 (离线测试用); 默认走 build_company_profile 与
    harness.mcp 注册表。out_dir 缺省 output/research/<ts_code>/。
    """
    if profile is None:
        from skills.content.profile import build_company_profile
        profile = build_company_profile(stock, years)
    if registry is None:
        from harness.mcp import get_registry
        registry = get_registry()

    ts_code = profile.get("ts_code") or ""
    name = profile.get("name") or stock
    sections = profile.get("sections", {})
    snap = (profile.get("generated_at") or "")[:10] or \
        datetime.now().strftime("%Y-%m-%d")

    extra = _fetch_extra_series(registry, stock, years)

    facts: List[Fact] = []
    facts += _facts_basic(sections, name, ts_code, snap)
    facts += _facts_mainbiz(sections)
    facts += _facts_financials(sections, extra, name, years)
    facts += _facts_valuation(sections, snap)
    facts += _facts_holders(sections)
    facts += _facts_industry(sections, name, ts_code, snap)
    for f in facts:
        f.ts_code = ts_code

    shard = EvidenceShard(
        kind="tushare", ts_code=ts_code, facts=facts,
        meta=_build_meta(profile, years, facts, snap))

    if out_dir is None:
        from core.paths import PathManager
        out_dir = research_dir(Path(PathManager().output_dir), ts_code)
    path = Path(out_dir) / "facts_tushare.json"
    save_shard(shard, path)
    shard.meta["path"] = str(path)
    logger.info(f"T02 事实分片落盘 {path} (facts={len(facts)})")
    return shard


def _build_meta(profile: Dict[str, Any], years: int, facts: List[Fact],
                snap: str) -> Dict[str, Any]:
    metrics_used = sorted({f.eid.split(".")[1] for f in facts
                           if f.eid.startswith("T:tushare.")})
    expected = {m["slug"] for m in _BASE_METRICS} | set(_EXTRA_BY_SLUG)
    missing = sorted(expected - set(metrics_used))
    if missing:
        logger.warning(f"以下指标全期无值 (数仓该列未回填), 已跳过: {missing}")
    profile_secs = sorted({f.eid.split(".")[1] for f in facts
                           if f.eid.startswith("T:profile.")})
    periods = sorted({f.period for f in facts})
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "stock": profile.get("stock", ""),
        "name": profile.get("name", ""),
        "ts_code": profile.get("ts_code", ""),
        "years": years,
        "snapshot_date": snap,
        "fact_count": len(facts),
        "view_count": 0,
        "sections_ok": profile.get("ok_sections", []),
        "sections_failed": profile.get("failed_sections", []),
        "coverage": {
            "tushare_metrics": metrics_used,
            "metrics_missing_data": missing,
            "profile_sections": profile_secs,
            "periods": periods,
        },
        "caliber": ("财务为合并报表(report_type=1)累计口径, 同年多期取年报行(12-31)优先, "
                    "无年报年份按实际季报期标注(Q1/H1/Q3 均为年初至期末累计); "
                    "金额字段原值单位元已÷1e8折算亿元(confidence=0.9, source注明); "
                    "期间标签 YYYYA=年报 YYYYH1=中报 YYYYQ1/Q3=季报"),
        "excluded": ("画像中 forecasts/verification/reports/risk/regime 板块未纳入: "
                     "属观点/判断层 (T04/T09 职责), 本分片纯事实层"),
    }


def _main() -> None:
    import argparse
    from collections import Counter
    ap = argparse.ArgumentParser(
        description="T02 Tushare/画像事实源 → facts_tushare.json (证据分片)")
    ap.add_argument("stock", help="股票名或代码, 如 贵州茅台")
    ap.add_argument("--years", type=int, default=5, help="时序年数 (默认 5)")
    ap.add_argument("--out", default="", help="输出目录 (默认 output/research/<ts_code>/)")
    args = ap.parse_args()

    shard = collect_tushare_facts(
        args.stock, args.years,
        out_dir=Path(args.out) if args.out else None)

    by_period = Counter(f.period for f in shard.facts)
    print(f"[T02] {shard.meta.get('name')} ({shard.ts_code}) "
          f"facts={len(shard.facts)} views=0")
    print(f"  期间分布: {dict(sorted(by_period.items()))}")
    print(f"  落盘: {shard.meta.get('path')}")


if __name__ == "__main__":
    _main()
