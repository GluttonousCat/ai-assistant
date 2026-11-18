"""Read-only inputs for industry mapping and segment metrics."""

from storage.pg import PgClient


def sw_members():
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            "SELECT si.con_code AS ts_code, ic.industry_name "
            "FROM stock.stock_industry si "
            "JOIN stock.index_classify ic ON si.index_code = ic.index_code "
            "WHERE si.out_date IS NULL AND si.is_new = 'Y'"
        )
    return rows


def stock_names():
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all("SELECT ts_code, name FROM stock.stock_basic")
    return rows


def recent_reports():
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            "SELECT ts_code, title, tags_industries, tags_themes, tags_targets, "
            "publish_date FROM fin.report_meta "
            "WHERE publish_date >= current_date - 365"
        )
    return rows


def latest_main_business():
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
    with PgClient(read_only=True) as pg:
        return pg.fetch_all(sql)


def chain_mentions(keywords):
    if not keywords:
        return 0
    clause = " OR ".join("segment ILIKE %s" for _ in keywords)
    with PgClient(read_only=True) as pg:
        row = pg.fetch_one(
            f"SELECT count(DISTINCT report_id) AS c FROM fin.chain_extract WHERE ({clause})",
            [f"%{keyword}%" for keyword in keywords],
        )
    return (row or {}).get("c", 0)


def market_inputs(all_codes, benchmark):
    with PgClient(read_only=True) as pg:
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
            (benchmark,),
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
    return daily, bench, mv, or_yoy
