# -*- encoding: utf-8 -*-
"""
Cninfo metadata/progress storage (pure files, no database).

Layout (output/cninfo/):
- meta/org_map.json     persistent sec_code -> orgId mapping (each stock is
                        truly resolved once; avoids re-hitting the
                        rate-limit-sensitive topSearch)
- meta/<ts_code>.json   per-stock announcement metadata + batch-crawl
                        checkpoint state
- downloads/<ts_code>/  PDF products (file on disk means downloaded)

Entry fields follow the historical PG table fin.cninfo_announcement
(pre-2026-10 version; the DDL used to live in storage/pg_schema.py); the
legacy table keeps the unmigrated historical data — the files are the
incremental source of truth.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict

META_ROOT = Path("output/cninfo/meta")


def _atomic_write(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, path)   # atomic replace (on Windows, rename raises when the target exists)


# ---------- orgId mapping ----------

def load_org_map() -> Dict[str, str]:
    try:
        return json.loads((META_ROOT / "org_map.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_org_map(m: Dict[str, str]) -> None:
    _atomic_write(META_ROOT / "org_map.json", m)


# ---------- per-stock metadata + checkpoint state ----------

def stock_meta_path(ts_code: str) -> Path:
    return META_ROOT / f"{ts_code}.json"


def load_stock_meta(ts_code: str) -> Dict:
    """{"announcements": {aid: entry}, "state": {"meta": ..., "dl": ..., "error": ...}}

    Each announcements entry: a cleaned projection of the raw announcement
    fields (title/category/report_year/announce_date/announce_ts/adjunct_url/
    sec_code/sec_name) plus download backfill fields (file_path/file_size/
    download_status/downloaded_at). state is maintained by the batch crawler
    (batch.py); the single-stock flow (downloader.sync_stock) never touches
    it.
    """
    try:
        meta = json.loads(stock_meta_path(ts_code).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    meta.setdefault("announcements", {})
    meta.setdefault("state", {})
    return meta


def save_stock_meta(ts_code: str, meta: Dict) -> None:
    _atomic_write(stock_meta_path(ts_code), meta)
