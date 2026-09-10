# -*- encoding: utf-8 -*-
"""
财务/行情域工具 (3个):
- query_financials        财务与行情查询 (单股/对比/行业/全市场排名, 底座 sql_builder)
- query_main_business     主营构成查询 (靠什么赚钱/收入结构)
- valuation_percentile    PE/PB 估值分位 (当前估值在历史中的水位)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from mcp.registry import REGISTRY
from mcp.spec import ToolError, obj_schema, param
from mcp.tools._common import df_payload, resolve_one, resolve_stocks


# 指标名 -> sql_builder 的 (field, prefix) 映射直接复用 FinQuerySkill 的别名表
def _metric_alias() -> Dict[str, Dict[str, str]]:
    from agent.skills.fin_query.skill import METRIC_ALIAS_FULL
    return METRIC_ALIAS_FULL


def _resolve_metric(metric: str):
    """中文指标 -> (裸字段名, 表前缀) ; sql_builder 需要 {prefix}.{bare_field}"""
    alias = _metric_alias()
    m = alias.get(metric) or alias.get(metric.strip())
    if not m:
        # 宽容: 去掉常见后缀再试
        for key in (metric.replace("率", ""), metric + "率"):
            if key in alias:
                m = alias[key]
                break
    if not m:
        raise ToolError(
            f"不支持的指标: 「{metric}」。支持: "
            + "、".join(sorted(alias)[:40]))
    dotted = m["field"]                    # 形如 'i.revenue' / 'db.pe_ttm'
    prefix, _, bare = dotted.partition(".")
    return bare, prefix


def _run_sql(sql: str) -> Dict[str, Any]:
    from tools.finance.sql_guard import validate_sql, force_limit
    sql = force_limit(sql, max_limit=1000)
    guard = validate_sql(sql)
    if not guard.valid:
        raise ToolError(f"SQL 校验失败: {guard.error}")
    from storage.pg import PgClient
    with PgClient() as pg:
        df = pg.fetch_df(sql)
    out = df_payload(df)
    out["sql"] = sql
    return out


# ============================================================
# 4. query_financials
# ============================================================

@REGISTRY.tool(
    name="query_financials",
    domain="financial",
    description=(
        "查财务指标与行情数据, 四种模式: single=单股时序(默认), compare=多股同指标对比, "
        "industry=申万行业内排名, rank=全市场排名/条件筛选。"
        "支持指标: 营收/净利润/毛利率/ROE/资产负债率/经营现金流/市盈率/市净率/总市值/"
        "收盘价/换手率等 40+ 项 (见 metric 参数说明)。"
        "时间表述如 '最近三年'/'2023年报' 会换算为报告期过滤。返回列名+数据行+实际执行的SQL。"),
    params_schema=obj_schema({
        "mode": param("查询模式", "string",
                      enum=["single", "compare", "industry", "rank"],
                      default="single"),
        "stocks": param("股票名或代码列表 (single 取第一个, compare 需 >=2 个)",
                        "array", items={"type": "string"}),
        "metrics": param("指标列表 (中文或英文别名, 如 ['营收','毛利率']; single 模式取前 3 个)",
                         "array", items={"type": "string"}),
        "industry": param("industry 模式的申万行业名 (如 '白酒'/'半导体')", "string"),
        "time": param("时间表述, 如 '最近三年'/'2024年报'/'2023'", "string"),
        "top_n": param("industry/rank 模式返回行数", "integer", default=10),
        "op": param("条件筛选运算符 (rank/industry 可选)",
                    "string", enum=["gt", "lt", "ge", "le"]),
        "value": param("op 对应的阈值数值", "number"),
    }),
    examples=[
        "查贵州茅台最近三年的营收和净利润",
        "茅台和五粮液谁的毛利率高",
        "白酒行业 ROE 排名前10",
        "全市场市值大于2000亿的股票",
    ],
)
def query_financials(mode: str = "single", stocks: List[str] = None,
                     metrics: List[str] = None, industry: str = "",
                     time: str = "", top_n: int = 10,
                     op: str = "", value: Optional[float] = None) -> Dict[str, Any]:
    from tools.finance.sql_builder import (
        build_compare_query, build_industry_query, build_rank_query,
        build_single_query,
    )

    stocks = stocks or []
    metrics = metrics or []
    if not metrics:
        raise ToolError("必须提供至少一个指标 (metrics), 如 ['营收']")

    # 时间表述 -> sql_builder 认的 (kw, val)
    time_phrase = None
    if time:
        from tools.finance.stock_kb import parse_time_phrase
        time_phrase = parse_time_phrase(time)

    if mode == "single":
        if not stocks:
            raise ToolError("single 模式必须提供 stocks")
        hit = resolve_one(stocks[0])
        field, prefix = _resolve_metric(metrics[0])
        extras = []
        for m in metrics[1:3]:
            extras.append(_resolve_metric(m))
        sql = build_single_query(
            hit["ts_code"], hit["name"], field, prefix,
            time_phrase, extra_fields=extras)
        if not sql:
            raise ToolError("SQL 构建失败, 请检查指标与时间表述")
        out = _run_sql(sql)
        out["stock"] = hit
        return out

    if mode == "compare":
        if len(stocks) < 2:
            raise ToolError("compare 模式需要 >=2 只股票")
        codes = []
        for s in stocks[:4]:
            hit = resolve_one(s)
            codes.append((hit["ts_code"], hit["name"]))
        field, prefix = _resolve_metric(metrics[0])
        sql = build_compare_query(codes, field, prefix, time_phrase)
        if not sql:
            raise ToolError("SQL 构建失败")
        return _run_sql(sql)

    if mode == "industry":
        if not industry:
            raise ToolError("industry 模式必须提供 industry (申万行业名)")
        from tools.finance.stock_kb import get_stock_kb
        kb = get_stock_kb()
        kb.load()
        # match_industry 需要行业语境词 (行业/板块), 工具入参只有行业名, 补齐
        idx = kb.match_industry(f"{industry}行业")
        if not idx:
            raise ToolError(
                f"未识别行业: {industry}。示例: 白酒/半导体/电池/光伏设备")
        index_code, _ind_name, level = idx
        field, prefix = _resolve_metric(metrics[0])
        sql = build_industry_query(
            index_code, field, prefix, time_phrase,
            top_n=int(top_n), level=level,
            op=op or None, value=value)
        if not sql:
            raise ToolError("SQL 构建失败")
        out = _run_sql(sql)
        out["industry"] = {"index_code": index_code, "level": level}
        return out

    if mode == "rank":
        field, prefix = _resolve_metric(metrics[0])
        sql = build_rank_query(field, prefix, top_n=int(top_n),
                               op=op or None, value=value, time_phrase=time_phrase)
        if not sql:
            raise ToolError("SQL 构建失败")
        return _run_sql(sql)

    raise ToolError(f"未知 mode: {mode}")


# ============================================================
# 5. query_main_business
# ============================================================

@REGISTRY.tool(
    name="query_main_business",
    domain="financial",
    description=(
        "查询主营构成 (靠什么赚钱): 按产品/地区/行业维度的收入、收入占比、毛利率。"
        "用户问 'XX靠什么赚钱'/'XX的收入结构'/'XX哪块业务占比最大'/'光模块业务占它收入多少' 时调用。"
        "数据来自财务报告披露的主营构成, 按最新报告期返回。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "biz_type": param("维度过滤: P=产品 D=地区 I=行业; 缺省全部", "string",
                          enum=["P", "D", "I"]),
        "top_n": param("每个维度返回条数", "integer", default=8),
    }, ["stock"]),
    examples=["贵州茅台靠什么赚钱", "中际旭创的收入结构", "宁德时代哪个业务占比最大"],
)
def query_main_business(stock: str, biz_type: str = "",
                        top_n: int = 8) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        latest = pg.fetch_one(
            "SELECT MAX(end_date) d FROM fin.v_main_biz WHERE ts_code=%s",
            (hit["ts_code"],))
        if not latest or not latest["d"]:
            raise ToolError(f"{hit['name']} 无主营构成数据")
        cond = "AND biz_type=%s" if biz_type else ""
        args: tuple = (hit["ts_code"], latest["d"])
        if biz_type:
            args = args + (biz_type,)
        df = pg.fetch_df(
            f"SELECT biz_type, bz_item, bz_sales, sales_share_pct, "
            f"       gross_margin_pct "
            f"FROM fin.v_main_biz "
            f"WHERE ts_code=%s AND end_date=%s AND NOT is_sub_item {cond} "
            f"ORDER BY bz_sales DESC NULLS LAST", args)
    out = df_payload(df, max_rows=int(top_n) * 3)
    out["stock"] = hit
    out["end_date"] = latest["d"].isoformat()
    out["biz_type_meaning"] = {"P": "产品", "D": "地区", "I": "行业"}
    return out


# ============================================================
# 6. valuation_percentile
# ============================================================

@REGISTRY.tool(
    name="valuation_percentile",
    domain="financial",
    description=(
        "估值分位: 个股当前 PE(TTM)/PB 处于近 N 年历史中的百分位 (0=最低 100=最高), "
        "并给出当前值。用户问 'XX现在估值贵不贵'/'估值什么水平'/'PE 分位' 时调用。"
        "与 query_financials 的区别: 那个查当期值, 本工具给历史相对水位。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "years": param("回看年数", "integer", default=3),
    }, ["stock"]),
    examples=["贵州茅台现在估值贵不贵", "中际旭创的PE分位", "宁德时代估值水位"],
)
def valuation_percentile(stock: str, years: int = 3) -> Dict[str, Any]:
    hit = resolve_one(stock)
    years = max(1, min(int(years), 10))
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT pe_ttm, pb FROM stock.daily_basic "
            "WHERE ts_code=%s AND trade_date >= current_date - %s "
            "ORDER BY trade_date", (hit["ts_code"], years * 365))
    if not rows:
        raise ToolError(f"{hit['name']} 无估值数据")

    def _pct(key: str) -> Optional[Dict[str, Any]]:
        arr = [float(r[key]) for r in rows
               if r.get(key) is not None and float(r[key]) > 0]
        if len(arr) < 60:
            return None
        latest = arr[-1]
        pct = sum(1 for v in arr if v < latest) / len(arr) * 100
        return {"latest": round(latest, 2),
                "pct_rank": round(pct, 1),
                "min": round(min(arr), 2), "max": round(max(arr), 2),
                "n_days": len(arr)}

    return {"stock": hit, "years": years,
            "pe_ttm": _pct("pe_ttm"), "pb": _pct("pb"),
            "verdict_hint": "pct_rank<30 偏低, 30-70 中性, >70 偏高 (相对自身历史)"}
