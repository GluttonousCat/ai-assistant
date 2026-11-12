# -*- encoding: utf-8 -*-
"""Tool-layer common helpers: stock resolution / DataFrame serialization / quotes loading."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd

from harness.mcp.spec import ToolError


def _jsonable(v: Any) -> Any:
    """Normalize PG NUMERIC->Decimal / date / NaN into JSON-safe types."""
    import datetime as _dt
    import decimal as _dec
    if isinstance(v, _dec.Decimal):
        return float(v)
    if isinstance(v, (_dt.date, _dt.datetime)):
        return v.isoformat()
    if isinstance(v, float):
        return None if pd.isna(v) else round(v, 4)
    if v is None:
        return None
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return v


def df_payload(df: pd.DataFrame, max_rows: int = 60) -> Dict[str, Any]:
    """DataFrame -> {columns, rows, data} (Decimal/NaN cleaned; truncated beyond the cap)."""
    if df is None or df.empty:
        return {"columns": list(df.columns) if df is not None else [],
                "rows": 0, "data": [], "truncated": False}
    truncated = len(df) > max_rows
    view = df.head(max_rows)
    data = [{k: _jsonable(v) for k, v in rec.items()}
            for rec in view.to_dict("records")]
    return {"columns": list(df.columns), "rows": int(len(df)),
            "data": data, "truncated": truncated}


def resolve_stocks(text: str, max_n: int = 5) -> List[Dict[str, str]]:
    """Resolve all stock entities from text [(ts_code, name, matched_alias)].
    Longest-match over the dictionary + mutually exclusive dedup (prevents '平安银行'
    and '平安' both hitting)."""
    from tools.finance.stock_kb import get_stock_kb
    kb = get_stock_kb()
    kb.load()
    text_l = (text or "").lower()
    hits: List[Tuple[int, int, str]] = []  # (start, len, alias)
    for alias in kb._stock_alias:
        if not alias or len(alias) < 2:
            continue
        idx = text_l.find(alias)
        if idx >= 0:
            # Skip aliases overlapping an already-hit span (keep the longer one)
            if any(not (idx + len(alias) <= s or idx >= s + l)
                   for s, l, _ in hits):
                continue
            hits.append((idx, len(alias), alias))
    hits.sort()
    out: List[Dict[str, str]] = []
    for _, _, alias in hits[:max_n]:
        code = kb._stock_alias.get(alias)
        if not code:
            continue
        out.append({"ts_code": code, "name": kb.get_name(code) or alias,
                    "matched": alias})
    if not out:
        # 6-digit code fallback (when the text contains 600519 / 300308.SZ directly)
        for m in re.finditer(r"(\d{6})(?:\.(SH|SZ|BJ))?", text or ""):
            sym, suf = m.group(1), m.group(2)
            if len(sym) != 6 or sym[0] not in "0369":
                continue
            suffix = f".{suf}" if suf else ""
            code = f"{sym}{suffix}"
            if not suffix:
                # No suffix: derive it by exchange rules
                code = (f"{sym}.SH" if sym[0] == "6" else
                        f"{sym}.BJ" if sym[0] in "48" else f"{sym}.SZ")
            name = kb.get_name(code)
            if name and all(o["ts_code"] != code for o in out):
                out.append({"ts_code": code, "name": name, "matched": sym})
    return out


def resolve_one(text: str) -> Dict[str, str]:
    """Resolve a single stock; raises ToolError on failure."""
    hits = resolve_stocks(text)
    if not hits:
        raise ToolError(
            f"未识别到股票实体: 「{text}」。请使用股票全名 (如 贵州茅台) 或 6 位代码 (如 600519)")
    return hits[0]


def stock_profile(ts_code: str) -> Dict[str, Any]:
    """Basic stock profile (including Shenwan industry)."""
    from storage.pg import PgClient
    with PgClient() as pg:
        row = pg.fetch_one(
            "SELECT ts_code, name, industry, industry_l1, industry_l2, "
            "       market, list_date, status "
            "FROM stock.stock_basic WHERE ts_code=%s", (ts_code,))
    if not row:
        raise ToolError(f"stock_basic 中不存在: {ts_code}")
    return {k: _jsonable(v) for k, v in row.items()}


def load_daily_bars(ts_code: str, days: int = 300) -> pd.DataFrame:
    """Daily bars (forward-adjusted, float8) for technical-indicator computation."""
    from storage.pg import PgClient
    with PgClient() as pg:
        df = pg.fetch_df(
            "SELECT trade_date, open::float8 AS open, high::float8 AS high, "
            "       low::float8 AS low, close::float8 AS close, "
            "       vol::float8 AS vol, amount::float8 AS amount "
            "FROM stock.daily WHERE ts_code=%s AND close IS NOT NULL "
            "ORDER BY trade_date DESC LIMIT %s", (ts_code, int(days)))
    if df.empty:
        raise ToolError(f"{ts_code} 无日K数据")
    return df.sort_values("trade_date").reset_index(drop=True)
