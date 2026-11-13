"""Listed A-share classification and current non-ST collection universe."""

import pandas as pd

from core.config import get_config


def board_of(code, market=None):
    if market in {"主板", "创业板", "科创板", "北交所"}:
        return market
    symbol, _, exchange = str(code).partition(".")
    if exchange == "BJ":
        return "北交所"
    if exchange == "SZ" and symbol.startswith(("300", "301")):
        return "创业板"
    if exchange == "SH" and symbol.startswith(("688", "689")):
        return "科创板"
    if (exchange == "SH" and symbol.startswith("60")) or (
        exchange == "SZ" and symbol.startswith(("000", "001", "002", "003"))
    ):
        return "主板"
    return "其他"


def risk_name(name):
    if name is None or pd.isna(name):
        return True
    value = str(name or "").upper().replace(" ", "")
    return not value or "ST" in value or "退" in value


def fetch_listed_universe(existing_bj=()):
    import tushare as ts

    pro = ts.pro_api(get_config().tushare_token, timeout=20)
    f = pro.stock_basic(
        list_status="L", fields="ts_code,name,industry,market,list_date"
    )
    if f is None or len(f) < 4000 or f.ts_code.duplicated().any():
        raise ValueError("上市股票名单不完整，拒绝替换股票池")
    f = f.copy()
    f["board"] = [board_of(r.ts_code, r.market) for r in f.itertuples()]
    f = f[
        ~f.name.map(risk_name)
        & (f.board.isin(["主板", "创业板", "科创板"]) | f.ts_code.isin(existing_bj))
    ].copy()
    f["industry"] = f.industry.fillna("未分类")
    f["list_date"] = pd.to_datetime(f.list_date, errors="raise").dt.date
    return f[["ts_code", "name", "industry", "board", "list_date"]]
