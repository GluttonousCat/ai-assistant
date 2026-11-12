# -*- encoding: utf-8 -*-
"""
Entity/knowledge domain tools (8):
- resolve_stock           stock entity resolution
- get_schema              data dictionary
- trading_calendar        A-share trading calendar
- query_top_holders       top-10 holders (holdings / changes / type)
- query_industry          Shenwan industry membership and same-industry constituents
- query_share_structure   share-structure history (total/float/free-float)
- get_session_history     historical conversation retrieval (trajectory replay)
- make_table              ad-hoc tabulation of data (markdown table)
"""
from __future__ import annotations

from typing import Any, Dict, List

from harness.mcp.registry import REGISTRY
from harness.mcp.spec import ToolError, obj_schema, param
from harness.mcp.tools._common import resolve_stocks, stock_profile


# ============================================================
# 1. resolve_stock
# ============================================================

@REGISTRY.tool(
    name="resolve_stock",
    domain="entity",
    description=(
        "股票身份与档案查询: 把简称/别名/6位代码解析为标准 ts_code, 返回名称、申万行业、"
        "上市信息。适用场景: ①用户问公司身份/行业归属 (如'宁德时代是哪个行业的'); "
        "②一句话提到多只股票需批量定位; ③别名拿不准需要确认。"
        "注意: 其他股票类工具都直接接受股票名并内部解析, **不要**把本工具当它们的前置步骤。"
        "只做实体定位, 不返回任何行情数据。"),
    params_schema=obj_schema({
        "text": param("包含股票称呼的原文片段, 如 '宁德时代和茅台的最新研报'", "string"),
    }, ["text"]),
    examples=["宁德时代是哪个行业的", "茅台和中际旭创", "600519 是什么公司"],
)
def resolve_stock(text: str) -> Dict[str, Any]:
    hits = resolve_stocks(text)
    if not hits:
        raise ToolError(f"未识别到股票实体: 「{text}」")
    out = []
    for h in hits:
        prof = stock_profile(h["ts_code"])
        out.append({**h, "industry": prof.get("industry"),
                    "industry_l1": prof.get("industry_l1"),
                    "industry_l2": prof.get("industry_l2"),
                    "market": prof.get("market")})
    return {"count": len(out), "stocks": out}


# ============================================================
# 2. get_schema
# ============================================================

@REGISTRY.tool(
    name="get_schema",
    domain="entity",
    description=(
        "查询本平台数据字典: 有哪些表、每个表有哪些字段、中文含义。"
        "当用户问题涉及数据范围确认 (如'你们有没有股东数据')、或需要为 SQL 查询确认字段名时调用。"
        "可传关键词过滤 (如表名片段或业务词, 如 'income'/'估值'/'研报')。"),
    params_schema=obj_schema({
        "keyword": param("过滤关键词, 可选; 匹配表名或字段描述", "string"),
    }),
    examples=["你们有什么数据", "财务三表有哪些字段", "估值数据在哪个表"],
)
def get_schema(keyword: str = "") -> Dict[str, Any]:
    from tools.finance.schema_info import get_schema_info
    text = get_schema_info().to_prompt_text()
    if keyword:
        lines = text.splitlines()
        kept, hit = [], False
        for ln in lines:
            if ln.startswith(("##", "表")) or "表:" in ln:
                hit = keyword.lower() in ln.lower()
            if not keyword or hit:
                kept.append(ln)
        text = "\n".join(kept) if kept else text
    return {"schema_text": text[:12000], "truncated": len(text) > 12000}


# ============================================================
# 3. trading_calendar
# ============================================================

@REGISTRY.tool(
    name="trading_calendar",
    domain="entity",
    description=(
        "A股交易日历: 最近交易日、T+N/T-N 偏移、日期区间内的交易日列表、各期期末交易日。"
        "涉及'昨天/上一交易日/最近一个月/季末'等时间表述需要换算成具体日期时调用。"
        "注意只处理交易日历, 不含财务报告期口径 (年报/季报截止日请用财务工具)。"),
    params_schema=obj_schema({
        "action": param("动作", "string", enum=["last", "offset", "range", "period_ends"]),
        "ref_date": param("基准日 YYYY-MM-DD, 缺省今天", "string"),
        "offset": param("offset 动作的交易日偏移量 (正=未来, 负=过去)", "integer"),
        "start": param("range/period_ends 动作的开始日 YYYY-MM-DD", "string"),
        "end": param("range/period_ends 动作的结束日 YYYY-MM-DD", "string"),
        "freq": param("period_ends 的频率: ME=月末 QE=季末 YE=年末", "string",
                      enum=["ME", "QE", "YE"], default="ME"),
    }, ["action"]),
    examples=["上一个交易日是哪天", "今天往后5个交易日", "上个月有几个交易日"],
)
def trading_calendar(action: str = "last", ref_date: str = "",
                     offset: int = 0, start: str = "", end: str = "",
                     freq: str = "ME") -> Dict[str, Any]:
    from tools.kline.calendar import (
        get_last_trading_day, get_offset_trading_day, get_period_ends,
        get_trading_days,
    )
    if action == "last":
        d = get_last_trading_day(ref_date or None)
        return {"action": action, "last_trading_day": d.isoformat()}
    if action == "offset":
        if not offset:
            raise ToolError("offset 动作必须提供非零 offset 参数")
        d = get_offset_trading_day(ref_date, int(offset))
        return {"action": action, "ref": ref_date, "offset": offset,
                "result": d.date().isoformat()}
    if action == "range":
        if not (start and end):
            raise ToolError("range 动作必须提供 start/end")
        df = get_trading_days(start, end)
        days = [d.date().isoformat() for d in df["date"]]
        return {"action": action, "count": len(days), "days": days[:250],
                "truncated": len(days) > 250}
    if action == "period_ends":
        if not (start and end):
            raise ToolError("period_ends 动作必须提供 start/end")
        ends = [d.date().isoformat() for d in get_period_ends(start, end, freq)]
        return {"action": action, "freq": freq, "ends": ends}
    raise ToolError(f"未知 action: {action}")


# ============================================================
# 4. query_top_holders (top-10 holders)
# ============================================================

@REGISTRY.tool(
    name="query_top_holders",
    domain="entity",
    description=(
        "十大股东查询: 最近报告期的十大股东名单、持股数量与比例、较上期增减、股东性质"
        "(基金/自然人/投资公司等), 并给出合计持股。"
        "用户问 'XX大股东是谁'/'XX股东变动'/'谁在增持XX' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "periods": param("回看报告期数 (每期10人)", "integer", default=1),
    }, ["stock"]),
    examples=["贵州茅台十大股东", "中际旭创最近股东变动"],
)
def query_top_holders(stock: str, periods: int = 1) -> Dict[str, Any]:
    from harness.mcp.tools._common import resolve_one
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        ends = [r["end_date"] for r in pg.fetch_all(
            "SELECT DISTINCT end_date FROM stock.top10_holders WHERE ts_code=%s "
            "ORDER BY end_date DESC LIMIT %s", (hit["ts_code"], int(periods)))]
        if not ends:
            raise ToolError(f"{hit['name']} 无十大股东数据")
        rows = pg.fetch_all(
            "SELECT end_date, holder_name, hold_amount, hold_ratio, "
            "       hold_float_ratio, hold_change, holder_type "
            "FROM stock.top10_holders WHERE ts_code=%s AND end_date = ANY(%s) "
            "ORDER BY end_date DESC, hold_ratio DESC NULLS LAST",
            (hit["ts_code"], ends))
    data = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif k in ("hold_amount", "hold_change") and v is not None:
                d[k] = round(float(v) / 1e4, 2)     # 10k shares -> 100M shares (亿)
            elif hasattr(v, "quantize"):
                d[k] = float(v)
            else:
                d[k] = v
        data.append(d)
    by_period: Dict[str, List[dict]] = {}
    for d in data:
        by_period.setdefault(d["end_date"], []).append(d)
    summary = {p: round(sum(x["hold_ratio"] or 0 for x in lst), 2)
               for p, lst in by_period.items()}
    return {"stock": hit, "hold_amount_unit": "亿股", "data": data,
            "top10_ratio_by_period": summary}


# ============================================================
# 5. query_industry (Shenwan industry)
# ============================================================

@REGISTRY.tool(
    name="query_industry",
    domain="entity",
    description=(
        "申万行业归属: 个股的申万一级/二级行业, 及同二级行业成分股清单(按最新总市值排序)。"
        "用户问 'XX属于什么行业'/'XX的同行业有哪些'/'申万二级某某行业成分股' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "peers_limit": param("同行业成分股返回数", "integer", default=15),
    }, ["stock"]),
    examples=["中际旭创属于什么行业", "贵州茅台的同行业公司"],
)
def query_industry(stock: str, peers_limit: int = 15) -> Dict[str, Any]:
    from harness.mcp.tools._common import resolve_one
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        base = pg.fetch_one(
            "SELECT industry, industry_l1, industry_l2 FROM stock.stock_basic "
            "WHERE ts_code=%s", (hit["ts_code"],))
        if not base or not base["industry_l2"]:
            raise ToolError(f"{hit['name']} 无申万行业数据")
        peers = pg.fetch_all(
            "SELECT DISTINCT ON (s.ts_code) s.ts_code, s.name, db.total_mv "
            "FROM stock.stock_basic s "
            "LEFT JOIN stock.daily_basic db ON db.ts_code = s.ts_code "
            "WHERE s.industry_l2 = %s "
            "ORDER BY s.ts_code, db.trade_date DESC",
            (base["industry_l2"],))
    peers.sort(key=lambda r: (r["total_mv"] is None,
                              -(float(r["total_mv"]) if r["total_mv"] else 0)))
    peer_list = [{"ts_code": r["ts_code"], "name": r["name"],
                  "total_mv_yi": (round(float(r["total_mv"]) / 1e4, 1)
                                  if r["total_mv"] else None)}
                 for r in peers[:int(peers_limit)]]
    return {"stock": hit,
            "industry_tushare": base["industry"],
            "sw_l1": base["industry_l1"], "sw_l2": base["industry_l2"],
            "peers_count": len(peers), "peers_top": peer_list,
            "total_mv_unit": "亿元"}


# ============================================================
# 6. query_share_structure (share structure)
# ============================================================

@REGISTRY.tool(
    name="query_share_structure",
    domain="entity",
    description=(
        "股本结构变迁: 近几年总股本/流通股本/自由流通股本的年度采样与变化率, "
        "配合分红送股可解释股本扩张来源。"
        "用户问 'XX总股本多少'/'XX股本怎么变化的'/'流通盘多大' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "years": param("回看年数", "integer", default=6),
    }, ["stock"]),
    examples=["宁德时代股本结构", "某公司总股本变化"],
)
def query_share_structure(stock: str, years: int = 6) -> Dict[str, Any]:
    from harness.mcp.tools._common import resolve_one
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT DISTINCT ON (date_trunc('year', trade_date)) "
            "       date_trunc('year', trade_date)::date AS year_start, "
            "       trade_date, total_share, float_share, free_share "
            "FROM stock.daily_basic "
            "WHERE ts_code=%s AND trade_date >= current_date - %s "
            "ORDER BY date_trunc('year', trade_date), trade_date DESC",
            (hit["ts_code"], int(years) * 366))
    if not rows:
        raise ToolError(f"{hit['name']} 无股本数据")
    data = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif v is not None and hasattr(v, "quantize"):
                d[k] = round(float(v) / 1e4, 2)    # 10k shares -> 100M shares (亿)
            else:
                d[k] = v
        data.append(d)
    data.sort(key=lambda d: d["trade_date"])
    for i in range(1, len(data)):
        prev, cur = data[i - 1]["total_share"], data[i]["total_share"]
        data[i]["total_share_yoy_pct"] = (
            round((cur - prev) / prev * 100, 2)
            if prev and cur else None)
    latest = data[-1]
    return {"stock": hit, "unit": "亿股", "data": data,
            "latest": {"total": latest["total_share"],
                       "float": latest["float_share"],
                       "free_float": latest["free_share"]}}


# ============================================================
# 7. get_session_history (historical conversation)
# ============================================================

@REGISTRY.tool(
    name="get_session_history",
    domain="entity",
    description=(
        "历史对话检索: 从本地轨迹日志回放最近的 Agent 对话(用户问了什么、回答了什么)。"
        "用户说 '我们之前聊过什么'/'上次问的那只股票是什么'/'看看历史对话' 时调用。"),
    params_schema=obj_schema({
        "limit": param("返回最近对话轮数", "integer", default=5),
        "keyword": param("关键词过滤 (可选)", "string"),
    }, []),
    examples=["我们之前聊过什么", "上次讨论贵州茅台的结论是什么"],
)
def get_session_history(limit: int = 5, keyword: str = "") -> Dict[str, Any]:
    import json
    from pathlib import Path
    from core.config import get_config
    tdir = Path(get_config().root_dir) / "logs" / "trajectories"
    if not tdir.exists():
        return {"turns": [], "note": "无轨迹日志"}
    files = sorted(tdir.glob("*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)[:60]
    turns = []
    for f in files:
        user_q, answer, ts = "", [], ""
        try:
            for line in f.open(encoding="utf-8"):
                try:
                    ev = json.loads(line)
                except (ValueError, KeyError):
                    continue
                et, payload = ev.get("event"), ev.get("data") or {}
                if et == "turn_start":
                    ts = (ev.get("ts") or "")[:19]
                    user_q = str(payload.get("input", ""))[:120]
                elif et == "delta" and payload.get("text"):
                    answer.append(payload["text"])
                elif et == "error":
                    answer.append(f"[错误] {payload.get('message', '')}")
        except OSError:
            continue
        if not user_q:
            continue
        ans = "".join(answer).strip()[:400]
        if keyword and keyword not in user_q and keyword not in ans:
            continue
        turns.append({"time": ts, "session": f.name.split("_")[0],
                      "question": user_q, "answer_brief": ans})
        if len(turns) >= int(limit):
            break
    return {"turns": turns, "count": len(turns),
            "note": "回答为节选(400字); 全量在 logs/trajectories/"}


# ============================================================
# 8. make_table (tabulation)
# ============================================================

@REGISTRY.tool(
    name="make_table",
    domain="entity",
    description=(
        "临时数据制表: 把手里的一组结构化数据(对象列表或二维数组)渲染成 markdown 表格。"
        "没有专用查询工具、又需要给用户呈现表格时使用。最多 30 行 x 12 列。"),
    params_schema=obj_schema({
        "rows": param("数据行: 对象列表 [{列:值}] 或二维数组", "array"),
        "columns": param("表头 (rows 为二维数组时必填)", "array"),
        "title": param("表格标题 (可选)", "string"),
    }, ["rows"]),
    examples=["把这组数据做成表格"],
)
def make_table(rows: list, columns: list = None, title: str = "") -> Dict[str, Any]:
    if not rows or not isinstance(rows, list):
        raise ToolError("rows 须为非空数组")
    if isinstance(rows[0], dict):
        cols = [str(c) for c in (columns or list(rows[0].keys()))]
        body = [[("" if r.get(c) is None else str(r.get(c))) for c in cols]
                for r in rows]
    elif isinstance(rows[0], (list, tuple)):
        if not columns:
            raise ToolError("二维数组必须提供 columns 表头")
        cols = [str(c) for c in columns]
        body = [[("" if v is None else str(v)) for v in r] for r in rows]
    else:
        raise ToolError("rows 元素须为对象或数组")
    if len(cols) > 12:
        cols, body = cols[:12], [b[:12] for b in body]
    if len(body) > 30:
        body = body[:30]
    md = ["| " + " | ".join(cols) + " |",
          "|" + "|".join(["---"] * len(cols)) + "|"]
    md += ["| " + " | ".join(row) + " |" for row in body]
    table = ("\n**" + title + "**\n" if title else "") + "\n".join(md)
    return {"title": title or None, "rows": len(body),
            "columns": cols, "markdown": table}
