"""Short, atomic report-analysis transactions; no model calls or background work."""

import re
from storage.pg import PgClient
from storage.schema.reports import ensure_report_analysis_columns

_COLUMNS = (
    "report_id, title, file_name, content_text, content_chars, ts_code, "
    "report_type, author, org_name, publish_date, market, symbols, file_path, "
    "extraction_status, analysis_json"
)
_UPDATE_FIELDS = frozenset(
    (
        "analysis_json",
        "market",
        "tags_industries",
        "tags_regions",
        "tags_commodities",
        "tags_themes",
        "tags_targets",
        "ts_code",
        "symbols",
        "report_type",
        "publish_date",
        "author",
        "org_name",
        "title",
    )
)


def extraction_candidates(report_id=None, limit=1):
    ensure_report_analysis_columns()
    with PgClient(read_only=True) as pg:
        if report_id:
            return pg.fetch_all(
                f"SELECT {_COLUMNS} FROM fin.report_meta WHERE report_id=%s",
                (int(report_id),),
            )
        return pg.fetch_all(
            f"SELECT {_COLUMNS} FROM fin.report_meta "
            "WHERE (content_chars > 200 OR file_path IS NOT NULL) "
            "AND (analysis_status IS NULL OR analysis_status NOT IN ('done','failed')) "
            "ORDER BY ts_code NULLS LAST, report_id LIMIT %s",
            (limit,),
        )


def load_report(report_id):
    ensure_report_analysis_columns()
    with PgClient(read_only=True) as pg:
        return pg.fetch_one(
            f"SELECT {_COLUMNS} FROM fin.report_meta WHERE report_id=%s",
            (int(report_id),),
        )


def mark_failed(report_id):
    with PgClient() as pg:
        pg.execute(
            "UPDATE fin.report_meta SET analysis_status='failed' "
            "WHERE report_id=%s AND analysis_status IS DISTINCT FROM 'done'",
            (report_id,),
        )


def save_extraction(report_id, fields, forecasts, *, previous_analysis):
    if not fields or fields.keys() - _UPDATE_FIELDS:
        raise ValueError("Invalid report metadata fields")
    with PgClient() as pg:
        assignments = [f"{key}=%s" for key in fields]
        changed = pg.execute(
            f"UPDATE fin.report_meta SET {', '.join(assignments)}, analysis_status='done' "
            "WHERE report_id=%s AND analysis_json IS NOT DISTINCT FROM %s",
            (*fields.values(), report_id, previous_analysis),
        )
        if changed == 0:
            return False
        # Replacement is atomic, including an empty forecast result.
        pg.execute("DELETE FROM fin.report_forecast WHERE report_id=%s", (report_id,))
        for forecast in forecasts:
            pg.execute(
                "INSERT INTO fin.report_forecast "
                "(report_id, ts_code, symbol, market, forecast_type, forecast_period, "
                "forecast_value, forecast_unit, confidence, raw_text) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    report_id,
                    forecast["ts_code"],
                    forecast["symbol"],
                    forecast["market"],
                    forecast["metric"],
                    forecast["period"],
                    forecast["value"],
                    forecast["unit"],
                    forecast["confidence"],
                    forecast["raw_text"],
                ),
            )
    return True


def matched_reports(target):
    ensure_report_analysis_columns()
    with PgClient(read_only=True) as pg:
        clause, args = _match_reports(pg, target)
        return pg.fetch_all(
            f"SELECT {_COLUMNS} FROM fin.report_meta WHERE {clause} AND content_chars > 0 "
            "ORDER BY publish_date DESC NULLS LAST LIMIT 10",
            tuple(args),
        )


def _match_reports(pg, target):
    """Returns (where_sql, args); (None, None) when nothing matches"""
    t = target.strip()

    # A-share: 6-digit code or exact name match
    ts_code = _resolve_a_share(pg, t)
    if ts_code:
        return "ts_code = %s", [ts_code]

    # US/HK ticker (e.g. NVDA.O / 0981.HK) or name: symbols LIKE match
    if re.match(r"^[A-Za-z0-9.\-]+$", t) and ("." in t or len(t) <= 6):
        if re.match(r"^[A-Za-z]{1,6}(\.[A-Z]{1,2})?$", t.upper()) or re.match(
            r"^\d{4,5}\.HK$", t
        ):
            return "symbols ILIKE %s", [f"%{t}%"]

    # commodity/theme keyword: match commodities/key_points in title or analysis_json
    return ("(title ILIKE %s OR analysis_json ILIKE %s)", [f"%{t}%", f"%{t}%"])


def _resolve_a_share(pg, target):
    """Stock name/code -> A-share ts_code (A-share universe only)"""
    t = target.strip().upper()
    if re.match(r"^\d{6}\.(SH|SZ|BJ)$", t):
        return t
    if re.match(r"^\d{6}$", t):
        suffix = (
            "SH"
            if t.startswith(("6", "9"))
            else "SZ"
            if t.startswith(("0", "3"))
            else "BJ"
        )
        return f"{t}.{suffix}"
    row = pg.fetch_one(
        "SELECT ts_code FROM stock.stock_basic WHERE name=%s", (target.strip(),)
    )
    return row["ts_code"] if row else None
