"""Fetch the latest available index-weight snapshots without database access."""
from datetime import date, timedelta
import pandas as pd
from core.config import get_config

INDEXES = {"000016.SH": 50, "000300.SH": 300, "000905.SH": 500,
           "000852.SH": 1000, "932000.CSI": 2000}


def fetch_constituents(as_of: date) -> pd.DataFrame:
    import tushare as ts
    pro = ts.pro_api(get_config().tushare_token, timeout=15)
    frames = []
    for code, expected in INDEXES.items():
        frame = pro.index_weight(index_code=code, start_date=(as_of-timedelta(days=90)).strftime("%Y%m%d"),
                                 end_date=as_of.strftime("%Y%m%d"))
        if frame is None or frame.empty:
            raise RuntimeError(f"指数 {code} 没有可用成分快照")
        frame = frame[frame.trade_date == frame.trade_date.max()].copy()
        if frame.con_code.nunique() != expected:
            raise RuntimeError(f"指数 {code} 成分数量异常: {frame.con_code.nunique()} / {expected}")
        frame = frame.rename(columns={"con_code": "ts_code", "trade_date": "as_of_date"})
        frame["as_of_date"] = pd.to_datetime(frame.as_of_date).dt.date
        frame["source"] = "tushare"
        frames.append(frame[["index_code", "as_of_date", "ts_code", "weight", "source"]])
    return pd.concat(frames, ignore_index=True)


def fetch_suspensions(codes: list[str], day: date) -> list[dict]:
    """Optional end-of-window audit; failure must not invent a suspension."""
    import tushare as ts
    pro = ts.pro_api(get_config().tushare_token, timeout=15)
    result = []
    for code in codes:
        frame = pro.suspend_d(ts_code=code, trade_date=day.strftime("%Y%m%d"))
        result.extend(frame.to_dict(orient="records"))
    return result
