"""Report catalog persistence. HTTP and model execution stay outside storage."""

from typing import Optional

from storage.pg import PgClient


def list_reports(
    ts_code: Optional[str] = None,
    keyword: Optional[str] = None,
    source: Optional[str] = None,
    analysis: Optional[str] = None,
    market: Optional[str] = None,
    industry: Optional[str] = None,
    region: Optional[str] = None,
    commodity: Optional[str] = None,
    theme: Optional[str] = None,
    target: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    page = max(1, page)
    page_size = min(max(1, page_size), 100)
    where = ["1=1"]
    args: list = []
    if source:
        where.append("source = %s")
        args.append(source)
    else:
        # Keep original-file and OCR visibility rules consistent with the
        # pre-refactor catalog, including the explicit-source override.
        where.append(
            "(file_name ~* '\\.(pdf|docx?)$' OR (file_name IS NULL AND file_path IS NULL AND content_chars > 0))"
        )
        where.append("extraction_status <> 'pending_ocr'")
    if market:
        where.append("market = %s")
        args.append(market)
    if industry:
        where.append(
            "(industry = %s OR ',' || COALESCE(tags_industries,'') || ',' LIKE %s)"
        )
        args.extend([industry, f"%,{industry},%"])
    if region:
        where.append("(region = %s OR ',' || COALESCE(tags_regions,'') || ',' LIKE %s)")
        args.extend([region, f"%,{region},%"])
    for col, val in (
        ("tags_commodities", commodity),
        ("tags_themes", theme),
        ("tags_targets", target),
    ):
        if val:
            where.append(f"(',' || COALESCE({col},'') || ',' LIKE %s)")
            args.append(f"%,{val},%")
    if keyword:
        where.append(
            "(title ILIKE %s OR COALESCE(author,'') ILIKE %s OR COALESCE(org_name,'') ILIKE %s)"
        )
        args.extend([f"%{keyword}%"] * 3)
    if analysis == "done":
        where.append("analysis_status = 'done'")
    elif analysis == "pending":
        where.append(
            "(analysis_status IS NULL OR analysis_status NOT IN ('done','failed'))"
        )
    where_sql = " AND ".join(where)
    with PgClient(read_only=True) as pg:
        total = pg.fetch_one(
            f"SELECT COUNT(*) AS n FROM fin.report_meta WHERE {where_sql}", tuple(args)
        )["n"]
        rows = pg.fetch_all(
            f"""SELECT report_id, topic_id, ts_code, title, author, org_name,
                       target, industry, region, market, publish_date, report_type,
                       source, file_name, content_chars, extraction_status,
                       analysis_status, symbols, tags_industries, tags_regions,
                       tags_commodities, tags_themes, tags_targets, created_at
                FROM fin.report_meta WHERE {where_sql}
                ORDER BY COALESCE(publish_date, created_at::date) DESC, report_id DESC
                LIMIT %s OFFSET %s""",
            tuple(args) + (page_size, (page - 1) * page_size),
        )
    for r in rows:
        if r.get("publish_date"):
            r["publish_date"] = r["publish_date"].strftime("%Y-%m-%d")
        if r.get("created_at"):
            r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M")
    return {"total": total, "page": page, "page_size": page_size, "items": rows}


def report_tags():
    """Aggregate dedicated classifications and comma-separated extraction tags."""
    with PgClient(read_only=True) as pg:
        industries = pg.fetch_all(
            "SELECT industry AS tag, COUNT(*) AS n FROM fin.report_meta "
            "WHERE industry IS NOT NULL AND industry <> '' "
            "GROUP BY 1 ORDER BY n DESC LIMIT 60"
        )
        regions = pg.fetch_all(
            "SELECT region AS tag, COUNT(*) AS n FROM fin.report_meta "
            "WHERE region IS NOT NULL AND region <> '' "
            "GROUP BY 1 ORDER BY n DESC LIMIT 60"
        )

        def _unnest(col: str) -> str:
            return f"""SELECT trim(split_part(t.v, ',', n.n)) AS tag, COUNT(*) AS n
                FROM (SELECT COALESCE({col},'') AS v FROM fin.report_meta
                      WHERE analysis_status='done') t,
                     generate_series(1, array_length(string_to_array(t.v, ','), 1)) AS n(n)
                WHERE trim(split_part(t.v, ',', n.n)) <> ''
                GROUP BY 1 ORDER BY n DESC LIMIT 60"""

        commodities = pg.fetch_all(_unnest("tags_commodities"))
        themes = pg.fetch_all(_unnest("tags_themes"))
        targets = pg.fetch_all(_unnest("tags_targets"))
    return {
        "industries": industries,
        "regions": regions,
        "commodities": commodities,
        "themes": themes,
        "targets": targets,
    }


def report_stocks():
    """Coverage by stock, retaining the catalog's top-200 ordering."""
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(
            """SELECT m.ts_code, MAX(b.name) AS name, COUNT(*) AS report_count,
                      MAX(COALESCE(m.publish_date, m.created_at::date)) AS latest_date,
                      (SELECT COUNT(*) FROM fin.report_forecast f
                       WHERE f.ts_code = m.ts_code) AS forecast_count
               FROM fin.report_meta m
               LEFT JOIN stock.stock_basic b ON b.ts_code = m.ts_code
               WHERE m.ts_code IS NOT NULL
               GROUP BY m.ts_code ORDER BY report_count DESC LIMIT 200"""
        )
    for r in rows:
        if r.get("latest_date"):
            r["latest_date"] = r["latest_date"].strftime("%Y-%m-%d")
    return {"items": rows}


def report_detail(report_id: int):
    with PgClient(read_only=True) as pg:
        row = pg.fetch_one(
            """SELECT report_id, topic_id, file_id, ts_code, title, author, org_name,
                      publish_date, report_type, source, file_name, file_path,
                      content_chars, content_text, content_text_cn, extraction_status,
                      analysis_status, analysis_json, created_at
               FROM fin.report_meta WHERE report_id=%s""",
            (report_id,),
        )
        if not row:
            raise LookupError("研报不存在")
        forecasts = pg.fetch_all(
            "SELECT forecast_type, forecast_period, forecast_value, forecast_unit, confidence, raw_text FROM fin.report_forecast WHERE report_id=%s ORDER BY forecast_period, forecast_type",
            (report_id,),
        )
    for k in ("publish_date", "created_at"):
        if row.get(k):
            row[k] = (
                row[k].strftime("%Y-%m-%d %H:%M")
                if k == "created_at"
                else row[k].strftime("%Y-%m-%d")
            )
    row["content_text"] = (
        row.pop("content_text_cn", None) or row.get("content_text") or ""
    )[:8000]
    row["forecasts"] = forecasts
    return row


def report_file(report_id):
    with PgClient(read_only=True) as pg:
        return pg.fetch_one(
            "SELECT file_name, file_path FROM fin.report_meta WHERE report_id=%s",
            (report_id,),
        )


def legacy_reports(source=None, status=None, limit=100):
    """Preserve the management endpoint's global total and extraction filter."""
    sql = """SELECT report_id, topic_id, file_id, ts_code, title, source,
                     extraction_status, content_chars, publish_date, created_at
              FROM fin.report_meta WHERE 1=1"""
    params = []
    if source:
        sql += " AND source = %s"
        params.append(source)
    if status:
        sql += " AND extraction_status = %s"
        params.append(status)
    sql += " ORDER BY report_id DESC LIMIT %s"
    params.append(min(limit, 500))
    with PgClient(read_only=True) as pg:
        rows = pg.fetch_all(sql, params)
        total = pg.fetch_one("SELECT count(*) AS c FROM fin.report_meta")["c"]
    return {"total": total, "reports": rows}


def legacy_detail(report_id, include_text=False):
    with PgClient(read_only=True) as pg:
        row = pg.fetch_one(
            "SELECT * FROM fin.report_meta WHERE report_id = %s", (report_id,)
        )
    if not row:
        raise LookupError("研报不存在")
    if not include_text:
        row.pop("content_text", None)
    return row


def add_report(ts_code, title, content):
    with PgClient() as pg:
        row = pg.fetch_one(
            """INSERT INTO fin.report_meta
                (ts_code, title, content_text, content_chars, source, extraction_status)
                VALUES (%s, %s, %s, %s, 'upload', 'extracted') RETURNING report_id""",
            (ts_code, title or content[:80], content, len(content)),
        )
    return {"status": "ok", "report_id": row["report_id"]}
