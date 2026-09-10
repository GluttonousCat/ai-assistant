"""
产业链分析原子能力 (Beta Skill 的映射层+量化层)

职责 (纯 SQL + pandas, 无 LLM 依赖, LLM 综述在 skill 层):
- 种子链模板加载与匹配 (beta_alpha/chains/*.yaml)
- 环节->标的三源映射: 主营构成占比(硬) / 申万L2(兜底) / 研报tags(佐证)
- 环节指数与超额收益: 成分股流通市值加权组合 vs 沪深300 (复权收益)
- 环节景气度: 成分股营收同比中位数 / 研报热度

用法(命令行验证):
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
BENCHMARK = "000300.SH"          # 沪深300
LOOKBACK_TRADE_DAYS = 120
EXCESS_SHORT_DAYS = 20
MIN_METRICS_MEMBERS = 2          # 成分股少于此数不算环节指数
MAX_INDEX_MEMBERS = 20           # 环节指数成分上限 (按主营占比降序)

# 分档阈值 (主营构成收入占比, %)
STRONG_SHARE = 30
MEDIUM_SHARE = 10

# 关键词裸泛词黑名单 (曾致误报, tests 与 forge 校验共用一份定义)
GENERIC_KEYWORD_BLACKLIST = {"整机", "树脂", "电子", "电机设备", "零部件"}

_chains_cache: Optional[List[Dict]] = None

# 素材缓存 (进程级): 回填期 fina_mainbz 已至百万行级, 逐环节全表查询会拖垮接口
# (曾致 Cloudflare 524); 改为一次索引扫描拉全量, 关键词匹配在内存完成
_MAINBZ_CACHE_TTL = 600       # 最新报告期主营构成缓存 10 分钟
_REPORTS_CACHE_TTL = 600      # 近一年研报标签索引缓存 10 分钟
_RESULT_TTL = 300             # 链条分析结果缓存 5 分钟 (页面刷新防抖)
_mainbz_cache: Optional[Tuple[float, Dict[str, Dict]]] = None
_reports_cache: Optional[Tuple[float, List[Dict]]] = None
_result_cache: Dict[Tuple[str, Optional[str]], Tuple[float, Dict]] = {}

_PROD_SUFFIX = re.compile(r"[（(]产品[)）]$")


# ============================================================
# 模板加载与匹配
# ============================================================

def load_chains(refresh: bool = False) -> List[Dict]:
    """加载全部种子链模板 (缓存)"""
    global _chains_cache
    if _chains_cache is not None and not refresh:
        return _chains_cache
    chains: List[Dict] = []
    for fp in sorted(CHAINS_DIR.glob("*.yaml")):
        if fp.stem.startswith("_"):
            continue  # _template.yaml 等非链文件
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
    """用户文本 -> 链模板 (name/desc/drivers/节点名/关键词 子串匹配)"""
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
    """用户文本 -> 链内节点 (节点名/id/关键词 子串匹配)"""
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
# 映射分档 (纯函数, 供 tests 验证)
# ============================================================

def assign_tier(share: Optional[float], sw_hit: bool, hits: int) -> str:
    """
    环节成员分档:
    - 强: 主营构成占比 >= 30%
    - 中: 占比 10~30%; 或无主营证据但申万L2命中且研报提及 >=2
    - 弱: 其余 (仅研报提及等弱证据)
    """
    if share is not None and share >= STRONG_SHARE:
        return "强"
    if (share is not None and share >= MEDIUM_SHARE) or (sw_hit and hits >= 2):
        return "中"
    return "弱"


# ============================================================
# SQL 片段
# ============================================================

def _kw_any_clause(col: str, keywords: List[str]) -> Tuple[str, List[str]]:
    """col ILIKE ANY 关键词 -> (clause, params)"""
    likes = " OR ".join([f"{col} ILIKE %s" for _ in keywords])
    return f"({likes})", [f"%{k}%" for k in keywords]


def _load_sw_members() -> Dict[str, str]:
    """申万 L2 当前成分: ts_code -> industry_name"""
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
    """全市场最新报告期 P 维度构成 (一次索引扫描; 走原始表绕开全表物化视图)

    返回 {ts_code: {end_date, items: [(bz_item, sales, is_sub)], total}}
    total = 非子项正收入合计 (占比分母); is_sub = 其中*/冒号分层 (与 v_main_biz 口径一致)
    """
    global _mainbz_cache
    if _mainbz_cache and time.time() - _mainbz_cache[0] < _MAINBZ_CACHE_TTL:
        return _mainbz_cache[1]

    sql = """
        WITH latest AS (
            SELECT ts_code, max(end_date) AS end_date
            FROM fin.fina_mainbz WHERE biz_type = 'P'
            GROUP BY ts_code
        )
        SELECT m.ts_code, l.end_date, m.bz_item, m.bz_sales
        FROM fin.fina_mainbz m
        JOIN latest l ON l.ts_code = m.ts_code AND m.end_date = l.end_date
        WHERE m.biz_type = 'P'
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
    """主营构成硬证据: 最新报告期 P 维度关键词命中 (内存匹配), ts_code -> {share, items}"""
    data = _load_latest_mainbz()
    kws = [k.lower() for k in keywords]
    out: Dict[str, Dict] = {}
    for code, d in data.items():
        if d["total"] <= 0:
            continue
        matched = []
        for item, sales, _sub in d["items"]:
            share = sales / d["total"] * 100
            if share < 5:      # 与原 SQL 口径一致: 单项占比 >=5 才参与
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
    """近一年研报标签索引 (tags/标题/代码), 供股票命中与环节热度内存计算"""
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
        # 热度口径不含 tags_targets
        r["blob_heat"] = " ".join(filter(None, [
            r.get("tags_industries"), r.get("tags_themes"),
            r.get("title")]) or "").lower()
    _reports_cache = (time.time(), rows)
    return rows


def _map_reports(keywords: List[str], days: int = 365) -> Dict[str, int]:
    """研报佐证: 近 N 天 tags/标题命中关键词的研报数, ts_code -> hits (内存匹配)"""
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
    """环节研报热度: 近 N 天全库命中关键词(tags/标题)的研报数 (内存匹配)"""
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
    """研报链抽取提及数 (fin.chain_extract; 表缺失时静默返回0)"""
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
# 环节指数 (批量: daily/daily_basic 主键 trade_date 开头, 按股票过滤是全表扫描,
# 逐环节查询在百万行级时曾致接口分钟级延迟 -> Cloudflare 524)
# ============================================================

def _batch_metrics(codes_by_node: Dict[str, List[str]]) -> Dict[str, Dict]:
    """全链环节指标一次算: 行情/基准/估值/财务各一次合并查询, 内存切环节。
    返回 {node_id: metrics}; 成分不足 2 只的环节不计。"""
    if not codes_by_node:
        return {}
    all_codes = sorted({c for cs in codes_by_node.values() for c in cs})
    try:
        with PgClient() as pg:
            daily = pg.fetch_all(
                "SELECT d.ts_code, d.trade_date, d.close, "
                "COALESCE(d.adj_factor, a.adj_factor, 1) AS adj_factor "
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

    # PG NUMERIC -> Decimal, 统一转 float; close_adj 预计算列可能未回填, 现算后复权价
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
        # fill_method=None: 停牌缺口留 NaN, 由 notna 掩码重分配权重 (不前向填充)
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
# 主入口: 链条分析
# ============================================================

def map_chain_members(chain: Dict, node_filter: Optional[Dict] = None,
                      top_members: int = 12, use_cache: bool = True) -> Dict:
    """
    环节->标的映射 + 环节指标.
    node_filter: 只算指定节点 (单环节下钻), None 算全链.
    use_cache: 命中 5 分钟内的相同请求直接返回 (页面刷新防抖; 强制重算传 False)
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
            # 仅行业归属不构成成员 (噪声大): 须有主营证据或研报提及
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

    # 环节指标批量一次算 (避免逐环节对 daily/daily_basic 的全表扫描)
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
    """链分析结果 -> 前端 data 事件平表 (环节x指标)"""
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
