"""Financial query persistence. SQL validation belongs to the tool boundary."""

import re
from typing import Optional
from storage.pg import PgClient


def query_frame(sql: str):
    with PgClient(read_only=True) as pg:
        return pg.fetch_df(sql)


def lookup_stock_code(name: str) -> Optional[str]:
    with PgClient(read_only=True) as pg:
        row = pg.fetch_one(
            "SELECT ts_code FROM stock.stock_basic WHERE name=%s", (name,)
        )
        if not row:
            row = pg.fetch_one(
                "SELECT ts_code FROM stock.stock_basic WHERE name LIKE %s LIMIT 1",
                (f"%{name}%",),
            )
        if not row and re.fullmatch(r"\d{6}", name):
            row = pg.fetch_one(
                "SELECT ts_code FROM stock.stock_basic WHERE symbol=%s", (name,)
            )
    return row["ts_code"] if row else None


def stock_profile_row(ts_code):
    with PgClient(read_only=True) as pg:
        row = pg.fetch_one(
            (
                "SELECT ts_code, name, industry, industry_l1, industry_l2,        "
                "market, list_date, status FROM stock.stock_basic WHERE ts_code=%s"
            ),
            (ts_code,),
        )
    return row


def daily_bars_frame(ts_code, days):
    with PgClient(read_only=True) as pg:
        df = pg.fetch_df(
            (
                "SELECT trade_date, open::float8 AS open, high::float8 AS high,"
                "        low::float8 AS low, close::float8 AS close,        "
                "vol::float8 AS vol, amount::float8 AS amount FROM stock.daily WHERE "
                "ts_code=%s AND close IS NOT NULL ORDER BY trade_date DESC LIMIT %s"
            ),
            (ts_code, int(days)),
        )
    return df


def valuation_history(ts_code, years):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT pe_ttm, pb FROM stock.daily_basic WHERE ts_code=%s AND "
                "trade_date >= current_date - %s ORDER BY trade_date"
            ),
            (ts_code, years * 365),
        )
    return rows


def dividend_history(ts_code, years, all_stages):
    cond = "" if all_stages else "AND div_proc = '实施'"
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT end_date, div_proc, stk_div, stk_bo_rate, stk_co_rate,"
                "        cash_div, cash_div_tax, record_date, ex_date, pay_date FROM "
                "fin.dividend WHERE ts_code=%s AND end_date >= current_date - %s "
                f"{cond}"
                " ORDER BY end_date DESC, ann_date DESC LIMIT 60"
            ),
            (ts_code, int(years) * 365),
        )
    return rows


def audit_history(ts_code, years):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT DISTINCT ON (end_date) end_date, ann_date, audit_result,"
                "        audit_agency, audit_fees, audit_sign FROM fin.fina_audit "
                "WHERE ts_code=%s AND end_date >= current_date - %s ORDER BY "
                "end_date, ann_date DESC LIMIT 40"
            ),
            (ts_code, int(years) * 365),
        )
    return rows


def cashflow_history(ts_code, periods):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT c.end_date, c.n_cashflow_act, c.n_cashflow_inv_act,        "
                "c.n_cash_flows_fnc_act, c.free_cashflow, c.c_fr_sale_sg,        "
                "i.n_income_attr_p, i.revenue FROM fin.cashflow c LEFT JOIN "
                "fin.income i ON i.ts_code=c.ts_code      AND i.end_date=c.end_date "
                "AND i.report_type=c.report_type WHERE c.ts_code=%s AND "
                "c.report_type='1' ORDER BY c.end_date DESC LIMIT %s"
            ),
            (ts_code, int(periods)),
        )
    return rows


def dupont_history(ts_code, years):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT end_date, roe, netprofit_margin, assets_turn,        "
                "assets_to_eqt, dp_assets_to_eqt, roa_dp, debt_to_assets FROM "
                "fin.fina_indicator WHERE ts_code=%s AND report_type='1'       AND "
                "date_part('month', end_date)=12 ORDER BY end_date DESC LIMIT %s"
            ),
            (ts_code, int(years)),
        )
    return rows


def share_history(ts_code, years):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT DISTINCT ON (date_trunc('year', trade_date))        "
                "date_trunc('year', trade_date)::date AS year_start,        "
                "trade_date, total_share, float_share, free_share FROM "
                "stock.daily_basic WHERE ts_code=%s AND trade_date >= current_date - "
                "%s ORDER BY date_trunc('year', trade_date), trade_date DESC"
            ),
            (ts_code, int(years) * 366),
        )
    return rows


def risk_inputs(ts_code):
    with PgClient(read_only=True) as pg:
        ind = pg.fetch_all(
            (
                "SELECT end_date, or_yoy, netprofit_yoy, grossprofit_margin,        "
                "debt_to_assets, profit_dedt, ocf_yoy FROM fin.fina_indicator WHERE "
                "ts_code=%s AND report_type='1' ORDER BY end_date DESC LIMIT 8"
            ),
            (ts_code,),
        )
        fin_rows = pg.fetch_all(
            (
                "SELECT i.end_date, i.revenue, i.n_income_attr_p,        "
                "i.net_after_nr_lp_correct, c.n_cashflow_act,        b.goodwill, "
                "b.total_hldr_eqy_exc_min_int, b.accounts_receiv,        "
                "b.total_assets FROM fin.income i LEFT JOIN fin.cashflow c ON "
                "i.ts_code=c.ts_code      AND i.end_date=c.end_date AND "
                "i.report_type=c.report_type LEFT JOIN fin.balancesheet b ON "
                "i.ts_code=b.ts_code      AND i.end_date=b.end_date AND "
                "i.report_type=b.report_type WHERE i.ts_code=%s AND i.report_type='1'"
                " ORDER BY i.end_date DESC LIMIT 8"
            ),
            (ts_code,),
        )
    return ind, fin_rows


def sell_side_forecasts(ts_code, days):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT r.org_name, r.publish_date, r.title,        f.forecast_type, "
                "f.forecast_period, f.forecast_value,        f.forecast_unit, "
                "f.confidence FROM fin.report_forecast f JOIN fin.report_meta r ON "
                "r.report_id = f.report_id WHERE f.ts_code=%s AND r.publish_date >= "
                "current_date - %s ORDER BY r.publish_date DESC LIMIT 300"
            ),
            (ts_code, int(days)),
        )
    return rows


def main_business(ts_code, stock_name, bz_code):
    with PgClient(read_only=True) as pg:
        latest = pg.fetch_one(
            "SELECT MAX(end_date) d FROM fin.v_main_biz WHERE ts_code=%s", (ts_code,)
        )
        if not latest or not latest["d"]:
            raise LookupError(f"{stock_name} 无主营构成数据")
        cond = "AND bz_code=%s" if bz_code else ""
        args: tuple = (ts_code, latest["d"])
        if bz_code:
            args = args + (bz_code,)
        df = pg.fetch_df(
            (
                "SELECT bz_code, bz_item, bz_sales, sales_share_pct,        "
                "gross_margin_pct FROM fin.v_main_biz WHERE ts_code=%s AND "
                "end_date=%s AND NOT is_sub_item "
                f"{cond}"
                " ORDER BY bz_sales DESC NULLS LAST"
            ),
            args,
        )
    return latest, df


def top_holders(ts_code, stock_name, periods):
    with PgClient(read_only=True) as pg:
        ends = [
            r["end_date"]
            for r in pg.fetch_all(
                "SELECT DISTINCT end_date FROM stock.top10_holders WHERE ts_code=%s ORDER BY end_date DESC LIMIT %s",
                (ts_code, int(periods)),
            )
        ]
        if not ends:
            raise LookupError(f"{stock_name} 无十大股东数据")
        rows = pg.fetch_all(
            (
                "SELECT end_date, holder_name, hold_amount, hold_ratio,        "
                "hold_float_ratio, hold_change, holder_type FROM stock.top10_holders "
                "WHERE ts_code=%s AND end_date = ANY(%s) ORDER BY end_date DESC, "
                "hold_ratio DESC NULLS LAST"
            ),
            (ts_code, ends),
        )
    return rows


def industry_members(ts_code, stock_name):
    with PgClient(read_only=True) as pg:
        base = pg.fetch_one(
            "SELECT industry, industry_l1, industry_l2 FROM stock.stock_basic WHERE ts_code=%s",
            (ts_code,),
        )
        if not base or not base["industry_l2"]:
            raise LookupError(f"{stock_name} 无申万行业数据")
        peers = pg.fetch_all(
            (
                "SELECT DISTINCT ON (s.ts_code) s.ts_code, s.name, db.total_mv FROM "
                "stock.stock_basic s LEFT JOIN stock.daily_basic db ON db.ts_code = "
                "s.ts_code WHERE s.industry_l2 = %s ORDER BY s.ts_code, db.trade_date"
                " DESC"
            ),
            (base["industry_l2"],),
        )
    return base, peers


def industry_financials(ts_code, stock_name):
    with PgClient(read_only=True) as pg:
        base = pg.fetch_one(
            "SELECT industry_l2 FROM stock.stock_basic WHERE ts_code=%s", (ts_code,)
        )
        if not base or not base["industry_l2"]:
            raise LookupError(f"{stock_name} 无申万行业数据")
        rows = pg.fetch_all(
            (
                "WITH peers AS (   SELECT ts_code, name FROM stock.stock_basic   "
                "WHERE industry_l2=%s ), mv AS (SELECT DISTINCT ON (ts_code) ts_code,"
                " pe_ttm, pb, total_mv        FROM stock.daily_basic        WHERE "
                "ts_code IN (SELECT ts_code FROM peers)        ORDER BY ts_code, "
                "trade_date DESC), fi AS (SELECT DISTINCT ON (ts_code) ts_code, roe, "
                "netprofit_yoy,               grossprofit_margin        FROM "
                "fin.fina_indicator        WHERE ts_code IN (SELECT ts_code FROM "
                "peers)              AND report_type='1'        ORDER BY ts_code, "
                "end_date DESC) SELECT p.ts_code, p.name, mv.pe_ttm, mv.pb, "
                "mv.total_mv,        fi.roe, fi.netprofit_yoy, fi.grossprofit_margin "
                "FROM peers p LEFT JOIN mv ON mv.ts_code=p.ts_code              LEFT "
                "JOIN fi ON fi.ts_code=p.ts_code"
            ),
            (base["industry_l2"],),
        )
    return base, rows


def disclosures_on(day):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT d.ts_code, s.name, d.end_date, d.pre_date, d.actual_date FROM"
                " fin.disclosure_date d LEFT JOIN stock.stock_basic s ON s.ts_code = "
                "d.ts_code WHERE d.actual_date=%s ORDER BY d.ts_code LIMIT 200"
            ),
            (day,),
        )
    return rows


def stock_disclosures(ts_code, periods):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT end_date, pre_date, actual_date, modify_date,        "
                "actual_date - pre_date AS delay_days FROM fin.disclosure_date WHERE "
                "ts_code=%s ORDER BY end_date DESC LIMIT %s"
            ),
            (ts_code, int(periods)),
        )
    return rows


def verification_forecasts(ts_code):
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            (
                "SELECT rf.forecast_type, rf.forecast_period, rf.forecast_value,"
                "        rf.forecast_unit, rm.org_name, rm.publish_date FROM "
                "fin.report_forecast rf LEFT JOIN fin.report_meta rm ON rm.report_id "
                "= rf.report_id WHERE rf.ts_code=%s AND rf.forecast_value IS NOT NULL"
                " ORDER BY rf.forecast_period"
            ),
            (ts_code,),
        )
    return rows


FORECAST_ACTUAL = {
    "revenue": ("fin.income", "revenue", "元"),
    "net_profit": ("fin.income", "n_income_attr_p", "元"),
    "eps": ("fin.income", "basic_eps", "元/股"),
    "roe": ("fin.fina_indicator", "roe", "%"),
    "gross_margin": ("fin.fina_indicator", "grossprofit_margin", "%"),
}


def forecast_actuals(ts_code, groups):
    result = {}
    with PgClient(read_only=True) as pg:
        for ftype, year in sorted(groups, key=lambda key: (key[1], key[0])):
            table, col, _ = FORECAST_ACTUAL[ftype]
            row = pg.fetch_one(
                f"SELECT {col} AS v FROM {table} WHERE ts_code=%s AND end_date=%s AND report_type='1' LIMIT 1",
                (ts_code, f"{year}-12-31"),
            )
            result[(ftype, year)] = row
    return result


def _forecast_divergence_rows(pg, ts_code):
    rows = pg.fetch_all(
        (
            "SELECT rf.forecast_type, rf.forecast_period, rf.forecast_value,"
            "        rf.forecast_unit, rm.publish_date, rm.org_name FROM "
            "fin.report_forecast rf LEFT JOIN fin.report_meta rm ON rm.report_id "
            "= rf.report_id WHERE rf.ts_code = %s AND rf.forecast_value IS NOT "
            "NULL ORDER BY rf.forecast_period DESC, rm.publish_date"
        ),
        (ts_code,),
    )
    return rows


def _financial_momentum_rows(pg, ts_code):
    rows = pg.fetch_all(
        (
            "SELECT end_date, or_yoy, netprofit_yoy, grossprofit_margin, "
            "q_sales_yoy FROM fin.fina_indicator WHERE ts_code = %s AND "
            "report_type = '1' ORDER BY end_date DESC LIMIT 8"
        ),
        (ts_code,),
    )
    return rows


def _valuation_context_rows(pg, ts_code):
    rows = pg.fetch_all(
        (
            "SELECT pe_ttm, pb FROM stock.daily_basic WHERE ts_code = %s AND "
            "trade_date >= current_date - 1095 ORDER BY trade_date"
        ),
        (ts_code,),
    )
    return rows


def _report_views_rows(pg, ts_code, name):
    rows = pg.fetch_all(
        (
            "SELECT title, org_name, publish_date, analysis_json FROM "
            "fin.report_meta WHERE (ts_code = %s OR title ILIKE %s OR "
            "tags_targets ILIKE %s)   AND publish_date IS NOT NULL ORDER BY "
            "publish_date DESC LIMIT 5"
        ),
        (ts_code, f"%{name}%", f"%{name}%"),
    )
    return rows


def alpha_inputs(ts_code, name):
    with PgClient(read_only=True) as pg:
        forecast = _forecast_divergence_rows(pg, ts_code)
        momentum = _financial_momentum_rows(pg, ts_code)
        valuation = _valuation_context_rows(pg, ts_code)
        views = _report_views_rows(pg, ts_code, name)
    return forecast, momentum, valuation, views


def stock_name(ts_code):
    with PgClient(read_only=True) as pg:
        row = pg.fetch_one(
            "SELECT name FROM stock.stock_basic WHERE ts_code=%s", (ts_code,)
        )
    return row["name"] if row else None


def forecast_context(ts_code, name):
    with PgClient(read_only=True) as pg:
        stats = _forecast_divergence_rows(pg, ts_code)
        raw = pg.fetch_all(
            (
                "SELECT rf.forecast_type, rf.forecast_period, rf.forecast_value,"
                "        rf.forecast_unit, rf.confidence, rm.org_name, "
                "rm.publish_date,        rm.title, rm.report_id FROM "
                "fin.report_forecast rf LEFT JOIN fin.report_meta rm ON rm.report_id "
                "= rf.report_id WHERE rf.ts_code=%s AND rf.forecast_value IS NOT NULL"
                " ORDER BY rf.forecast_period DESC, rm.publish_date DESC LIMIT %s"
            ),
            (ts_code, 20),
        )
        views = _report_views_rows(pg, ts_code, name)
    return stats, raw, views


def holder_snapshot(ts_code):
    with PgClient(read_only=True) as pg:
        latest = pg.fetch_one(
            "SELECT MAX(end_date) d FROM stock.top10_holders WHERE ts_code=%s",
            (ts_code,),
        )
        if not latest or not latest["d"]:
            return {}
        rows = pg.fetch_all(
            "SELECT holder_name, hold_ratio, hold_float_ratio,        hold_change, holder_type, end_date FROM stock.top10_holders WHERE ts_code=%s AND end_date=%s ORDER BY hold_ratio DESC NULLS LAST LIMIT 10",
            (ts_code, latest["d"]),
        )
        return {"end_date": str(latest["d"]), "holders": [dict(r) for r in rows]}
