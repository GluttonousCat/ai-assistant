# -*- encoding: utf-8 -*-
"""
财务/行情域工具 (8个):
- query_financials        财务与行情查询 (单股/对比/行业/全市场排名, 底座 sql_builder)
- query_main_business     主营构成查询 (靠什么赚钱/收入结构)
- valuation_percentile    PE/PB 估值分位 (当前估值在历史中的水位)
- query_dividend          分红送股历史 (派息/送转/登记日/除权日)
- query_audit_opinion     审计意见 (结论/事务所/费用/签字会计师)
- disclosure_calendar     财报披露日历 (预约/实际披露/延期)
- analyze_cashflow        现金流质量 (三净额/自由现金流/净现比/收现比)
- dupont_analysis         杜邦分解 (ROE三因子与驱动定位)
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from harness.mcp.registry import REGISTRY
from harness.mcp.spec import ToolError, obj_schema, param
from harness.mcp.tools._common import df_payload, resolve_one


# 指标名 -> sql_builder 的 (field, prefix) 映射直接复用 FinQuerySkill 的别名表
def _metric_alias() -> Dict[str, Dict[str, str]]:
    from skills.fin_query.skill import METRIC_ALIAS_FULL
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
        "bz_code": param("维度过滤: P=产品 D=地区 I=行业; 缺省全部", "string",
                         enum=["P", "D", "I"]),
        "top_n": param("每个维度返回条数", "integer", default=8),
    }, ["stock"]),
    examples=["贵州茅台靠什么赚钱", "中际旭创的收入结构", "宁德时代哪个业务占比最大"],
)
def query_main_business(stock: str, bz_code: str = "",
                        top_n: int = 8) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        latest = pg.fetch_one(
            "SELECT MAX(end_date) d FROM fin.v_main_biz WHERE ts_code=%s",
            (hit["ts_code"],))
        if not latest or not latest["d"]:
            raise ToolError(f"{hit['name']} 无主营构成数据")
        cond = "AND bz_code=%s" if bz_code else ""
        args: tuple = (hit["ts_code"], latest["d"])
        if bz_code:
            args = args + (bz_code,)
        df = pg.fetch_df(
            f"SELECT bz_code, bz_item, bz_sales, sales_share_pct, "
            f"       gross_margin_pct "
            f"FROM fin.v_main_biz "
            f"WHERE ts_code=%s AND end_date=%s AND NOT is_sub_item {cond} "
            f"ORDER BY bz_sales DESC NULLS LAST", args)
    out = df_payload(df, max_rows=int(top_n) * 3)
    out["stock"] = hit
    out["end_date"] = latest["d"].isoformat()
    out["bz_code_meaning"] = {"P": "产品", "D": "地区", "I": "行业"}
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


# ============================================================
# 7. query_dividend 分红除权
# ============================================================

@REGISTRY.tool(
    name="query_dividend",
    domain="financial",
    description=(
        "分红送股历史: 历年每股派息(税前/税后)、送股转增比例、股权登记日、除权除息日。"
        "用户问 'XX分红多不多'/'XX历次分红'/'XX除权日'/'股息记录' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "years": param("回看年数", "integer", default=10),
        "all_stages": param("含预案/决案等未实施阶段 (默认只看已实施)", "boolean",
                            default=False),
    }, ["stock"]),
    examples=["贵州茅台历年分红", "中际旭创最近一次除权日"],
)
def query_dividend(stock: str, years: int = 10,
                   all_stages: bool = False) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from datetime import date as _date, timedelta as _td
    from storage.pg import PgClient
    cond = "" if all_stages else "AND div_proc = '实施'"
    with PgClient() as pg:
        rows = pg.fetch_all(
            f"SELECT end_date, div_proc, stk_div, stk_bo_rate, stk_co_rate, "
            f"       cash_div, cash_div_tax, record_date, ex_date, pay_date "
            f"FROM fin.dividend "
            f"WHERE ts_code=%s AND end_date >= current_date - %s {cond} "
            f"ORDER BY end_date DESC, ann_date DESC LIMIT 60",
            (hit["ts_code"], int(years) * 365))
    if not rows:
        raise ToolError(f"{hit['name']} 无分红记录")
    floor = (_date.today() - _td(days=365)).isoformat()
    data, cash_12m = [], 0.0
    for r in rows:
        d = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif hasattr(v, "quantize"):
                d[k] = float(v)
            else:
                d[k] = v
        if d["ex_date"] and d["ex_date"] >= floor:
            cash_12m += float(d["cash_div_tax"] or 0)
        data.append(d)
    return {"stock": hit, "rows": len(data), "data": data,
            "cash_div_tax_12m_per_share": round(cash_12m, 4),
            "note": "金额为每股元; 近12月按除权日口径合计(税前)"}


# ============================================================
# 8. query_audit_opinion 审计意见
# ============================================================

@REGISTRY.tool(
    name="query_audit_opinion",
    domain="financial",
    description=(
        "审计意见查询: 历年审计结论(标准无保留/保留意见等)、会计师事务所、审计费用、"
        "签字会计师变更。审计意见恶化是财务可信度的重要信号。"
        "用户问 'XX审计意见'/'XX财报可信吗'/'审计机构换过吗' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "years": param("回看年数", "integer", default=15),
    }, ["stock"]),
    examples=["贵州茅台审计意见", "某公司审计机构变更历史"],
)
def query_audit_opinion(stock: str, years: int = 15) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT DISTINCT ON (end_date) end_date, ann_date, audit_result, "
            "       audit_agency, audit_fees, audit_sign "
            "FROM fin.fina_audit "
            "WHERE ts_code=%s AND end_date >= current_date - %s "
            "ORDER BY end_date, ann_date DESC LIMIT 40",
            (hit["ts_code"], int(years) * 365))
    if not rows:
        raise ToolError(f"{hit['name']} 无审计意见数据")
    data = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif k == "audit_fees" and v is not None:
                d[k] = round(float(v) / 1e4, 1)
            elif hasattr(v, "quantize"):
                d[k] = float(v)
            else:
                d[k] = v
        data.append(d)
    non_std = [d for d in data
               if d["audit_result"] and "标准无保留" not in d["audit_result"]]
    return {"stock": hit, "rows": len(data), "data": data,
            "audit_fees_unit": "万元",
            "warnings": [f"{d['end_date']} 报告期: {d['audit_result']}"
                         for d in non_std] or ["历年均标准无保留意见"]}


# ============================================================
# 9. disclosure_calendar 财报披露
# ============================================================

@REGISTRY.tool(
    name="disclosure_calendar",
    domain="financial",
    description=(
        "财报披露日历: 个股各报告期的预约披露日/实际披露日/延期天数, 或某日全市场"
        "实际披露了哪些公司。用户问 'XX年报什么时候出'/'XX财报延期了吗'/"
        "'今天有哪些公司披露财报' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码 (与 date 二选一)", "string"),
        "date": param("查询该日实际披露的公司 (YYYYMMDD, 与 stock 二选一)", "string"),
        "periods": param("按股查询时回看报告期数", "integer", default=8),
    }, []),
    examples=["贵州茅台下次财报什么时候披露", "20260331 披露了哪些年报"],
)
def disclosure_calendar(stock: str = "", date: str = "",
                        periods: int = 8) -> Dict[str, Any]:
    from datetime import date as _date, datetime as _dt
    from storage.pg import PgClient
    if not stock and not date:
        raise ToolError("stock 与 date 至少提供一个")
    with PgClient() as pg:
        if date:
            d = _dt.strptime(date.replace("-", ""), "%Y%m%d").date()
            rows = pg.fetch_all(
                "SELECT d.ts_code, s.name, d.end_date, d.pre_date, d.actual_date "
                "FROM fin.disclosure_date d "
                "LEFT JOIN stock.stock_basic s ON s.ts_code = d.ts_code "
                "WHERE d.actual_date=%s ORDER BY d.ts_code LIMIT 200", (d,))
            data = []
            for r in rows:
                data.append({k: (v.isoformat() if hasattr(v, "isoformat") else v)
                             for k, v in r.items()})
            return {"date": d.isoformat(), "count": len(data), "data": data}
        hit = resolve_one(stock)
        rows = pg.fetch_all(
            "SELECT end_date, pre_date, actual_date, modify_date, "
            "       actual_date - pre_date AS delay_days "
            "FROM fin.disclosure_date WHERE ts_code=%s "
            "ORDER BY end_date DESC LIMIT %s",
            (hit["ts_code"], int(periods)))
    if not rows:
        raise ToolError(f"{hit['name']} 无披露计划数据")
    today_s = _date.today().isoformat()
    data = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif hasattr(v, "quantize"):
                d[k] = float(v)
            else:
                d[k] = v
        data.append(d)
    upcoming = next((d for d in data
                     if d["pre_date"] and d["pre_date"] >= today_s
                     and not d["actual_date"]), None)
    delays = sum(1 for d in data if d["delay_days"] and d["delay_days"] > 0)
    return {"stock": hit, "data": data,
            "next_scheduled": upcoming, "delayed_periods": delays}


# ============================================================
# 10. analyze_cashflow 现金流分析
# ============================================================

@REGISTRY.tool(
    name="analyze_cashflow",
    domain="financial",
    description=(
        "现金流质量分析: 经营/投资/筹资三净额、自由现金流、销售收现, 并派生净现比"
        "(经营现金流/净利润)与收现比(销售收现/营收)。净现比持续<1 说明利润含金量差。"
        "用户问 'XX现金流怎么样'/'利润是真钱吗'/'XX赚的钱收回来了吗' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "periods": param("回看报告期数", "integer", default=8),
    }, ["stock"]),
    examples=["贵州茅台现金流质量", "某公司利润含金量怎么样"],
)
def analyze_cashflow(stock: str, periods: int = 8) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT c.end_date, c.n_cashflow_act, c.n_cashflow_inv_act, "
            "       c.n_cash_flows_fnc_act, c.free_cashflow, c.c_fr_sale_sg, "
            "       i.n_income_attr_p, i.revenue "
            "FROM fin.cashflow c "
            "LEFT JOIN fin.income i ON i.ts_code=c.ts_code "
            "     AND i.end_date=c.end_date AND i.report_type=c.report_type "
            "WHERE c.ts_code=%s AND c.report_type='1' "
            "ORDER BY c.end_date DESC LIMIT %s",
            (hit["ts_code"], int(periods)))
    if not rows:
        raise ToolError(f"{hit['name']} 无现金流数据")

    def _f(v):
        return float(v) if v is not None and hasattr(v, "quantize") else v

    def _ratio(a, b):
        try:
            a, b = float(a), float(b)
            return round(a / b, 3) if b else None
        except (TypeError, ValueError):
            return None

    yi = ("n_cashflow_act", "n_cashflow_inv_act", "n_cash_flows_fnc_act",
          "free_cashflow", "c_fr_sale_sg", "n_income_attr_p", "revenue")
    data = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            else:
                d[k] = _f(v)
        d["ocf_to_profit"] = _ratio(r["n_cashflow_act"], r["n_income_attr_p"])
        d["sales_cash_to_rev"] = _ratio(r["c_fr_sale_sg"], r["revenue"])
        for k in yi:
            if d.get(k) is not None:
                d[k] = round(d[k] / 1e8, 2)   # 元 -> 亿元
        data.append(d)
    verdict = None
    ratios = [d["ocf_to_profit"] for d in data if d["ocf_to_profit"] is not None]
    if ratios:
        verdict = ("利润含金量好" if ratios[0] >= 1 else
                   "利润含金量偏弱" if ratios[0] >= 0.5 else "利润含金量差")
    return {"stock": hit, "unit": "亿元", "data": data,
            "ocf_to_profit_latest": data[0]["ocf_to_profit"],
            "verdict": verdict}


# ============================================================
# 11. dupont_analysis 杜邦分析
# ============================================================

@REGISTRY.tool(
    name="dupont_analysis",
    domain="financial",
    description=(
        "杜邦分解: ROE = 销售净利率 x 总资产周转率 x 权益乘数, 按年报口径逐期分解, "
        "定位 ROE 变化的驱动因子 (是赚得多/转得快/还是加杠杆)。"
        "用户问 'XX的ROE为什么变化'/'XX靠什么赚钱效率'/'杜邦分析' 时调用。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "years": param("回看年报数", "integer", default=5),
    }, ["stock"]),
    examples=["贵州茅台ROE为什么这么高", "某公司杜邦分析"],
)
def dupont_analysis(stock: str, years: int = 5) -> Dict[str, Any]:
    hit = resolve_one(stock)
    from storage.pg import PgClient
    with PgClient() as pg:
        rows = pg.fetch_all(
            "SELECT end_date, roe, netprofit_margin, assets_turn, "
            "       assets_to_eqt, dp_assets_to_eqt, roa_dp, debt_to_assets "
            "FROM fin.fina_indicator "
            "WHERE ts_code=%s AND report_type='1' "
            "      AND date_part('month', end_date)=12 "
            "ORDER BY end_date DESC LIMIT %s",
            (hit["ts_code"], int(years)))
    if not rows:
        raise ToolError(f"{hit['name']} 无财务指标数据")
    data = []
    for r in rows:
        d = {}
        for k, v in r.items():
            if hasattr(v, "isoformat"):
                d[k] = v.isoformat()
            elif hasattr(v, "quantize"):
                d[k] = round(float(v), 4)
            else:
                d[k] = v
        if None not in (d["netprofit_margin"], d["assets_turn"],
                        d["dp_assets_to_eqt"]):
            d["product_check"] = round(d["netprofit_margin"] * d["assets_turn"]
                                       * d["dp_assets_to_eqt"], 2)
        else:
            d["product_check"] = None
        data.append(d)
    data.reverse()   # 旧 -> 新
    drivers = None
    if len(data) >= 2:
        prev, cur = data[-2], data[-1]
        keys = ("netprofit_margin", "assets_turn", "dp_assets_to_eqt")
        if all(prev.get(k) is not None and cur.get(k) is not None for k in keys):
            nm0, at0, eq0 = (prev["netprofit_margin"], prev["assets_turn"],
                             prev["dp_assets_to_eqt"])
            nm1, at1, eq1 = (cur["netprofit_margin"], cur["assets_turn"],
                             cur["dp_assets_to_eqt"])
            contrib = {"净利率驱动": round((nm1 - nm0) * at0 * eq0, 2),
                       "周转率驱动": round(nm0 * (at1 - at0) * eq0, 2),
                       "杠杆驱动": round(nm0 * at0 * (eq1 - eq0), 2)}
            drivers = {
                "contrib_pp": contrib,
                "dominant": max(contrib, key=lambda k: abs(contrib[k])),
                "roe_change_pp": (round(cur["roe"] - prev["roe"], 2)
                                  if None not in (cur.get("roe"), prev.get("roe"))
                                  else None)}
    return {"stock": hit, "basis": "年报口径", "data": data,
            "factor_decomposition": drivers,
            "note": "product_check=三因子乘积应接近roe; 分解为相邻年报一阶近似"}
