"""
Industry-chain analysis atomic capability (mapping layer + quant layer of Beta Skill)

Responsibilities (pure SQL + pandas, no LLM dependency; the LLM summary lives in the skill layer):
- Seed-chain template loading and matching (beta_alpha/chains/*.yaml)
- Segment -> constituent mapping via three sources: main-business composition
  share (hard evidence) / Shenwan L2 (fallback) / report tags (corroboration)
- Segment index and excess return: free-float-market-cap weighted portfolio of
  constituents vs CSI 300 (adjusted returns)
- Segment prosperity: median revenue YoY of constituents / report heat

CLI verification:
    python -m agent.beta_alpha.analysis.chain_analysis ai_compute
"""
from __future__ import annotations

import copy
import re
import time
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd
import yaml

from core.logger import get_logger
from storage.pg import PgClient

logger = get_logger(__name__)

CHAINS_DIR = Path(__file__).resolve().parents[1] / "chains"
BENCHMARK = "000300.SH"          # CSI 300
LOOKBACK_TRADE_DAYS = 120
EXCESS_SHORT_DAYS = 20
MIN_METRICS_MEMBERS = 2          # a segment index needs at least this many constituents
MAX_INDEX_MEMBERS = 20           # cap on segment index constituents (by main-business share, desc)

# Tiering thresholds (main-business revenue share, %)
STRONG_SHARE = 30
MEDIUM_SHARE = 10

# Blacklist of bare generic keywords (once caused false positives); one shared
# definition used by tests and forge validation
GENERIC_KEYWORD_BLACKLIST = {"整机", "树脂", "电子", "电机设备", "零部件"}

_chains_cache: Optional[List[Dict]] = None

# Material caches (process level): fina_mainbz has grown to millions of rows in
# the backfill period; per-segment full-table queries would drag the API down
# (once caused a Cloudflare 524). Instead: one indexed scan pulls everything,
# keyword matching happens in memory.
_MAINBZ_CACHE_TTL = 600       # latest-report-period main-business cache: 10 minutes
_REPORTS_CACHE_TTL = 600      # last-year report tag index cache: 10 minutes
_RESULT_TTL = 300             # chain analysis result cache: 5 minutes (page-refresh debounce)
_mainbz_cache: Optional[Tuple[float, Dict[str, Dict]]] = None
_reports_cache: Optional[Tuple[float, List[Dict]]] = None
_result_cache: Dict[Tuple[str, Optional[str]], Tuple[float, Dict]] = {}

_PROD_SUFFIX = re.compile(r"[（(]产品[)）]$")


# ============================================================
# Template loading and matching
# ============================================================

def load_chains(refresh: bool = False) -> List[Dict]:
    """Load all seed-chain templates (cached)"""
    global _chains_cache
    if _chains_cache is not None and not refresh:
        return _chains_cache
    chains: List[Dict] = []
    for fp in sorted(CHAINS_DIR.glob("*.yaml")):
        if fp.stem.startswith("_"):
            continue  # non-chain files such as _template.yaml
        try:
            with open(fp, encoding="utf-8") as f:
                data = yaml.safe_load(f)
            if data and data.get("chain_id") and data.get("nodes"):
                chains.append(data)
        except Exception as e:
            logger.warning(f"链模板加载失败 {fp.name}: {e}")
    _chains_cache = chains
    return chains


def get_chain(chain_id: str) -> Optional[Dict]:
    for c in load_chains():
        if c["chain_id"] == chain_id:
            return c
    return None


def match_chain(text: str) -> Optional[Dict]:
    """User text -> chain template (substring match on name/desc/drivers/node names/keywords)"""
    if not text:
        return None
    for c in load_chains():
        fields = [c.get("name", ""), c.get("chain_id", ""), c.get("desc", "")]
        fields += [str(d) for d in c.get("drivers", [])]
        for node in c.get("nodes", []):
            fields += [node.get("name", ""), node.get("id", "")]
        if any(text in f or f in text for f in fields if f and len(f) >= 2):
            return c
    return None


def find_node(chain: Dict, text: str) -> Optional[Dict]:
    """User text -> node within a chain (substring match on node name/id/keywords)"""
    if not text:
        return None
    for node in chain.get("nodes", []):
        fields = [node.get("name", ""), node.get("id", "")]
        fields += [str(k) for k in node.get("keywords", [])]
        for f in fields:
            if f and len(f) >= 2 and (f in text or text in f):
                return node
    return None


# ============================================================
# Tier assignment (pure function, verified by tests)
# ============================================================

def assign_tier(share: Optional[float], sw_hit: bool, hits: int) -> str:
    """
    Segment member tiering:
    - strong: main-business composition share >= 30%
    - medium: share 10~30%; or no main-business evidence but Shenwan L2 hit with >= 2 report mentions
    - weak: everything else (only weak evidence such as report mentions)
    """
    if share is not None and share >= STRONG_SHARE:
        return "强"
    if (share is not None and share >= MEDIUM_SHARE) or (sw_hit and hits >= 2):
        return "中"
    return "弱"


# ============================================================
# SQL fragments
# ============================================================

def _kw_any_clause(col: str, keywords: List[str]) -> Tuple[str, List[str]]:
    """col ILIKE ANY over keywords -> (clause, params)"""
    likes = " OR ".join([f"{col} ILIKE %s" for _ in keywords])
    return f"({likes})", [f"%{k}%" for k in keywords]


def _load_sw_members() -> Dict[str, str]:
    """Current Shenwan L2 constituents: ts_code -> industry_name"""
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT si.con_code AS ts_code, ic.industry_name "
            "FROM stock.stock_industry si "
            "JOIN stock.index_classify ic ON si.index_code = ic.index_code "
            "WHERE si.out_date IS NULL AND si.is_new = 'Y'"
        )
    return {r["ts_code"]: r["industry_name"] for r in rows}


def _load_stock_names() -> Dict[str, str]:
    with PgClient() as pg:
        rows = pg.fetch_all("SELECT ts_code, name FROM stock.stock_basic")
    return {r["ts_code"]: r["name"] for r in rows}


def _load_latest_mainbz() -> Dict[str, Dict]:
    """Whole-market latest-report-period P-dimension composition (one indexed scan;
    hits the raw table to bypass the full-table materialized view)

    Returns {ts_code: {end_date, items: [(bz_item, sales, is_sub)], total}}
    total = sum of positive non-sub-item revenue (denominator for shares);
    is_sub = the "其中*" / colon layering (same caliber as v_main_biz)
    """
    global _mainbz_cache
    if _mainbz_cache and time.time() - _mainbz_cache[0] < _MAINBZ_CACHE_TTL:
        return _mainbz_cache[1]

    sql = """
        WITH latest AS (
            SELECT ts_code, max(end_date) AS end_date
            FROM fin.fina_mainbz WHERE bz_code = 'P'
            GROUP BY ts_code
        )
        SELECT m.ts_code, l.end_date, m.bz_item, m.bz_sales
        FROM fin.fina_mainbz m
        JOIN latest l ON l.ts_code = m.ts_code AND m.end_date = l.end_date
        WHERE m.bz_code = 'P'
          AND m.bz_sales IS NOT NULL AND m.bz_sales <> 0
          AND m.bz_item NOT IN ('产品', '行业', '地区')
          AND m.bz_item NOT LIKE '%合计%'
          AND m.bz_item NOT LIKE '%小计%'
          AND m.bz_item <> '-'
    """
    t0 = time.time()
    with PgClient() as pg:
        rows = pg.fetch_all(sql)
    by_stock: Dict[str, Dict] = {}
    for r in rows:
        item = str(r["bz_item"]).strip()
        item = _PROD_SUFFIX.sub("", item)
        sales = float(r["bz_sales"])
        is_sub = item.startswith("其中") or (":" in item) or ("：" in item)
        d = by_stock.setdefault(
            r["ts_code"], {"end_date": str(r["end_date"]), "items": [], "total": 0.0})
        d["items"].append((item, sales, is_sub))
        if not is_sub and sales > 0:
            d["total"] += sales
    _mainbz_cache = (time.time(), by_stock)
    logger.info(f"主营构成素材加载: {len(rows)} 行 / {len(by_stock)} 只 "
                f"({time.time() - t0:.1f}s, 缓存{_MAINBZ_CACHE_TTL}s)")
    return by_stock


def _map_mainbz(keywords: List[str]) -> Dict[str, Dict]:
    """Hard evidence from main-business composition: keyword hits in the latest
    report period, P dimension (in-memory matching); ts_code -> {share, items}"""
    data = _load_latest_mainbz()
    kws = [k.lower() for k in keywords]
    out: Dict[str, Dict] = {}
    for code, d in data.items():
        if d["total"] <= 0:
            continue
        matched = []
        for item, sales, _sub in d["items"]:
            share = sales / d["total"] * 100
            if share < 5:      # same caliber as the original SQL: only items with share >= 5 count
                continue
            if any(k in item.lower() for k in kws):
                matched.append((item, share))
        if matched:
            matched.sort(key=lambda x: -x[1])
            out[code] = {
                "share": round(min(100.0, sum(s for _, s in matched)), 1),
                "items": [i for i, _ in matched[:3]],
                "end_date": d["end_date"],
            }
    return out


def _load_reports_index() -> List[Dict]:
    """Last-year report tag index (tags/title/code) for in-memory stock matching
    and segment-heat computation"""
    global _reports_cache
    if _reports_cache and time.time() - _reports_cache[0] < _REPORTS_CACHE_TTL:
        return _reports_cache[1]
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT ts_code, title, tags_industries, tags_themes, tags_targets, "
            "publish_date FROM fin.report_meta "
            "WHERE publish_date >= current_date - 365")
    for r in rows:
        r["blob"] = " ".join(filter(None, [
            r.get("tags_industries"), r.get("tags_themes"),
            r.get("tags_targets"), r.get("title")]) or "").lower()
        # Heat caliber deliberately excludes tags_targets
        r["blob_heat"] = " ".join(filter(None, [
            r.get("tags_industries"), r.get("tags_themes"),
            r.get("title")]) or "").lower()
    _reports_cache = (time.time(), rows)
    return rows


def _map_reports(keywords: List[str], days: int = 365) -> Dict[str, int]:
    """Report corroboration: count of reports in the last N days whose
    tags/title hit the keywords; ts_code -> hits (in-memory matching)"""
    cutoff = date.today() - timedelta(days=days)
    kws = [k.lower() for k in keywords]
    hits: Dict[str, int] = {}
    for r in _load_reports_index():
        pd_ = r.get("publish_date")
        if not pd_ or pd_ < cutoff or not r.get("ts_code"):
            continue
        if any(k in r["blob"] for k in kws):
            hits[r["ts_code"]] = hits.get(r["ts_code"], 0) + 1
    return hits


def _node_report_heat(keywords: List[str], days: int = 180) -> int:
    """Segment report heat: count of reports in the last N days repo-wide whose
    tags/title hit the keywords (in-memory matching)"""
    cutoff = date.today() - timedelta(days=days)
    kws = [k.lower() for k in keywords]
    n = 0
    for r in _load_reports_index():
        pd_ = r.get("publish_date")
        if not pd_ or pd_ < cutoff:
            continue
        if any(k in r["blob_heat"] for k in kws):
            n += 1
    return n


_chain_extract_exists: Optional[bool] = None


def _node_chain_mentions(keywords: List[str]) -> int:
    """Mention count from report chain extraction (fin.chain_extract; silently
    returns 0 when the table is missing)"""
    global _chain_extract_exists
    if _chain_extract_exists is False:
        return 0
    clause, params = _kw_any_clause("segment", keywords)
    try:
        with PgClient() as pg:
            r = pg.fetch_one(
                f"SELECT count(DISTINCT report_id) AS c FROM fin.chain_extract WHERE {clause}",
                params,
            )
            _chain_extract_exists = True
            return (r or {}).get("c", 0)
    except Exception:
        _chain_extract_exists = False
        return 0


# ============================================================
# Segment index (batched: daily/daily_basic are keyed on trade_date first, so
# filtering by stock is a full-table scan; per-segment queries once caused
# minute-level API latency at millions of rows -> Cloudflare 524)
# ============================================================

def _batch_metrics(codes_by_node: Dict[str, List[str]]) -> Dict[str, Dict]:
    """Compute metrics for all chain segments at once: one merged query each for
    quotes/benchmark/valuation/financials, split per segment in memory.
    Returns {node_id: metrics}; segments with fewer than 2 constituents are skipped."""
    if not codes_by_node:
        return {}
    all_codes = sorted({c for cs in codes_by_node.values() for c in cs})
    try:
        with PgClient() as pg:
            daily = pg.fetch_all(
                "SELECT d.ts_code, d.trade_date, d.close, "
                "COALESCE(a.adj_factor, 1) AS adj_factor "
                "FROM stock.daily d "
                "LEFT JOIN stock.adj_factor a "
                "  ON a.ts_code = d.ts_code AND a.trade_date = d.trade_date "
                "WHERE d.ts_code = ANY(%s) AND d.trade_date >= current_date - 300 "
                "ORDER BY d.trade_date",
                (all_codes,),
            )
            bench = pg.fetch_all(
                "SELECT trade_date, close FROM stock.index_daily "
                "WHERE ts_code = %s AND trade_date >= current_date - 300 "
                "ORDER BY trade_date",
                (BENCHMARK,),
            )
            mv = pg.fetch_all(
                "SELECT DISTINCT ON (ts_code) ts_code, circ_mv FROM stock.daily_basic "
                "WHERE ts_code = ANY(%s) ORDER BY ts_code, trade_date DESC",
                (all_codes,),
            )
            or_yoy = pg.fetch_all(
                "SELECT DISTINCT ON (ts_code) ts_code, or_yoy FROM fin.fina_indicator "
                "WHERE ts_code = ANY(%s) AND report_type = '1' "
                "ORDER BY ts_code, end_date DESC",
                (all_codes,),
            )
    except Exception as e:
        logger.warning(f"环节指标批量计算失败: {e}")
        return {}
    if not daily or not bench:
        return {}

    # PG NUMERIC -> Decimal, uniformly cast to float; adjustment factor from
    # stock.adj_factor, adjusted price computed on the fly
    df = pd.DataFrame(daily)
    df["price"] = (df["close"].astype(float) * df["adj_factor"].astype(float))
    wide_all = df.pivot(index="trade_date", columns="ts_code", values="price").sort_index()

    bdf = pd.DataFrame(bench)
    bdf["close"] = bdf["close"].astype(float)
    bdf = bdf.set_index("trade_date")["close"].tail(LOOKBACK_TRADE_DAYS + 10)
    bench_ret = float(bdf.iloc[-1]) / float(bdf.iloc[0]) - 1 if len(bdf) >= 2 else None
    b_short = bdf.tail(EXCESS_SHORT_DAYS + 1)
    bench_short = (float(b_short.iloc[-1]) / float(b_short.iloc[0]) - 1
                   if len(b_short) >= 2 else None)

    mv_map = {r["ts_code"]: float(r["circ_mv"] or 0) for r in mv}
    yoy_map = {r["ts_code"]: float(r["or_yoy"]) for r in or_yoy
               if r["or_yoy"] is not None}

    def _excess(p, b):
        return (1 + p) / (1 + b) - 1 if (p is not None and b is not None) else None

    out: Dict[str, Dict] = {}
    for node_id, codes in codes_by_node.items():
        cols = [c for c in codes if c in wide_all.columns]
        if len(cols) < MIN_METRICS_MEMBERS:
            continue
        wide = wide_all[cols].tail(LOOKBACK_TRADE_DAYS + 5)
        if len(wide) < EXCESS_SHORT_DAYS + 5:
            continue
        w = pd.Series({c: max(mv_map.get(c, 0.0), 1.0) for c in wide.columns})
        # fill_method=None: suspension gaps stay NaN, and the notna mask
        # redistributes weights (no forward filling)
        ret = wide.pct_change(fill_method=None)
        wr = (ret.mul(w, axis=1).sum(axis=1)
              / ret.notna().mul(w, axis=1).sum(axis=1)).dropna()
        if len(wr) < EXCESS_SHORT_DAYS + 2:
            continue
        port_full = float((1 + wr).prod() - 1)
        port_short = float((1 + wr.tail(EXCESS_SHORT_DAYS)).prod() - 1)
        yoy_vals = [yoy_map[c] for c in cols if c in yoy_map]
        out[node_id] = {
            "port_ret_120d": round(port_full * 100, 2),
            "excess_120d": round(_excess(port_full, bench_ret) * 100, 2)
                if _excess(port_full, bench_ret) is not None else None,
            "excess_20d": round(_excess(port_short, bench_short) * 100, 2)
                if _excess(port_short, bench_short) is not None else None,
            "or_yoy_median": round(float(pd.Series(yoy_vals).median()), 2)
                if yoy_vals else None,
            "members_used": cols,
        }
    return out


# ============================================================
# Main entry: chain analysis
# ============================================================

def map_chain_members(chain: Dict, node_filter: Optional[Dict] = None,
                      top_members: int = 12, use_cache: bool = True) -> Dict:
    """
    Segment -> constituent mapping + segment metrics.
    node_filter: compute only the given node (single-segment drill-down);
    None computes the whole chain.
    use_cache: identical requests within 5 minutes return the cached copy
    (page-refresh debounce; pass False to force recomputation)
    """
    cache_key = (chain.get("chain_id", ""), node_filter["id"] if node_filter else None)
    if use_cache and cache_key in _result_cache:
        ts, cached = _result_cache[cache_key]
        if time.time() - ts < _RESULT_TTL:
            return copy.deepcopy(cached)

    names = _load_stock_names()
    sw = _load_sw_members()

    nodes_out: List[Dict] = []
    index_members_map: Dict[str, List[str]] = {}
    for node in chain.get("nodes", []):
        if node_filter and node["id"] != node_filter["id"]:
            continue
        kws = [str(k) for k in node.get("keywords", [])]
        mainbz = _map_mainbz(kws)
        reports = _map_reports(kws)
        sw_list = [str(s) for s in node.get("sw_l2", [])]

        members: List[Dict] = []
        all_codes = set(mainbz) | set(reports)
        for code in all_codes:
            mb = mainbz.get(code)
            share = mb["share"] if mb else None
            hits = reports.get(code, 0)
            sw_name = sw.get(code, "")
            sw_hit = any(s in sw_name for s in sw_list) if sw_name else False
            # Industry membership alone does not make a member (too noisy):
            # main-business evidence or a report mention is required
            if not (mb or hits >= 1):
                continue
            tier = assign_tier(share, sw_hit, hits)
            evidence = []
            if mb:
                evidence.append(f"主营: {'/'.join(mb['items'])} 占比{share}% ({mb['end_date']})")
            if sw_hit:
                evidence.append(f"申万L2: {sw_name}")
            if hits:
                evidence.append(f"近1年研报 {hits} 篇")
            members.append({
                "ts_code": code,
                "name": names.get(code, code),
                "mainbz_share": share,
                "mainbz_items": "/".join(mb["items"]) if mb else None,
                "sw_industry": sw_name if sw_hit else None,
                "report_hits": hits,
                "tier": tier,
                "evidence": "; ".join(evidence),
            })

        members.sort(key=lambda m: ({"强": 0, "中": 1, "弱": 2}[m["tier"]],
                                    -(m["mainbz_share"] or 0), -m["report_hits"]))
        index_members_map[node["id"]] = [
            m["ts_code"] for m in members
            if m["tier"] in ("强", "中")][:MAX_INDEX_MEMBERS]

        nodes_out.append({
            "id": node["id"],
            "name": node.get("name", node["id"]),
            "keywords": kws,
            "members": members[:top_members],
            "strong_count": sum(1 for m in members if m["tier"] == "强"),
            "medium_count": sum(1 for m in members if m["tier"] == "中"),
            "weak_count": sum(1 for m in members if m["tier"] == "弱"),
            "metrics": None,
            "report_heat_6m": _node_report_heat(kws),
            "chain_mentions": _node_chain_mentions(kws),
        })

    # Segment metrics computed in one batch (avoids per-segment full-table scans on daily/daily_basic)
    metrics_map = _batch_metrics(index_members_map)
    for n in nodes_out:
        n["metrics"] = metrics_map.get(n["id"])

    result = {
        "chain": {
            "chain_id": chain["chain_id"],
            "name": chain.get("name", chain["chain_id"]),
            "desc": chain.get("desc", ""),
            "drivers": chain.get("drivers", []),
            "edges": chain.get("edges", []),
        },
        "nodes": nodes_out,
        "benchmark": BENCHMARK,
        "node_filter": node_filter["id"] if node_filter else None,
    }
    _result_cache[cache_key] = (time.time(), copy.deepcopy(result))
    return result


def nodes_to_table(result: Dict) -> Tuple[List[str], List[Dict]]:
    """Chain analysis result -> flat table for the frontend data event (segment x metric)"""
    cols = ["node", "node_name", "strong", "medium", "port_ret_120d",
            "excess_120d", "excess_20d", "or_yoy_median", "report_heat_6m"]
    rows = []
    for n in result.get("nodes", []):
        m = n.get("metrics") or {}
        rows.append({
            "node": n["name"],
            "node_name": n["name"],
            "strong": n["strong_count"],
            "medium": n["medium_count"],
            "port_ret_120d": m.get("port_ret_120d"),
            "excess_120d": m.get("excess_120d"),
            "excess_20d": m.get("excess_20d"),
            "or_yoy_median": m.get("or_yoy_median"),
            "report_heat_6m": n.get("report_heat_6m"),
        })
    return cols, rows


if __name__ == "__main__":
    import sys
    chain_id = sys.argv[1] if len(sys.argv) > 1 else "ai_compute"
    chain = get_chain(chain_id)
    if not chain:
        print(f"链不存在: {chain_id}, 可用: {[c['chain_id'] for c in load_chains()]}")
        raise SystemExit(1)
    res = map_chain_members(chain)
    print(f"\n== {res['chain']['name']} (基准 {BENCHMARK}) ==\n")
    cols, rows = nodes_to_table(res)
    print(pd.DataFrame(rows, columns=[c for c in cols if c != "node_name"]).to_string(index=False))
    for n in res["nodes"]:
        top = [f"{m['name']}({m['tier']}|{m['mainbz_share'] or 0}%)" for m in n["members"][:5]]
        print(f"\n[{n['name']}] 强{n['strong_count']}/中{n['medium_count']}/弱{n['weak_count']} "
              f"研报热度{n['report_heat_6m']} => {'; '.join(top)}")
