"""
日K数据加载层: universe 筛选 + 前复权行情批量拉取

数据来源: stock.daily。表内 adj_factor / close_adj 预计算列当前尚未回填 (全 NULL),
因此前复权价格用 pct_chg 累计收益重构 (与 tushare qfq 数学等价):
    ratio[t] = cumprod(1 + pct_chg)[:t] / cumprod[-1]   (以窗口最后一日为基准, 末位=1)
除权除息跳空被 pct_chg (基于除权后昨收) 正确吸收; 待 adj_factor 回填后可切换实现。

SQL 内显式 ::float8 转换, 避免 NUMERIC -> Decimal 在 pandas 中退化为 object 列。
全市场约 5000 只 / 千万行, 因此扫描一律按批次拉取 (batch_size), 不整表加载。
"""
from __future__ import annotations

from datetime import date, timedelta
from typing import Iterator, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

from core.logger import get_logger

logger = get_logger("range_trading.loader")

_SQL_DAILY = """
SELECT ts_code, trade_date,
       open::float8  AS open,
       high::float8  AS high,
       low::float8   AS low,
       close::float8 AS close,
       pct_chg::float8 AS pct_chg,
       vol::float8       AS vol,
       amount::float8    AS amount
FROM stock.daily
WHERE ts_code = ANY(%s)
  AND trade_date >= %s AND trade_date <= %s
  AND close IS NOT NULL
ORDER BY ts_code, trade_date
"""


def _adjust_forward(g: pd.DataFrame) -> pd.DataFrame:
    """单标的内做前复权: 全部价格列乘以同一缩放序列, 末位基准 = 1"""
    ret = g["pct_chg"].fillna(0.0).to_numpy(dtype=float) / 100.0
    ratio = np.cumprod(1.0 + ret)
    ratio = ratio / ratio[-1] if ratio[-1] > 0 else np.ones_like(ratio)
    g = g.copy()
    for col in ("open", "high", "low", "close"):
        g[col] = g[col].to_numpy(dtype=float) * ratio
    return g


def get_latest_trade_date(pg, on_or_before: Optional[date] = None) -> Optional[date]:
    """最新交易日 (或指定日及之前的最新交易日)"""
    if on_or_before is None:
        row = pg.fetch_one("SELECT MAX(trade_date) AS d FROM stock.daily")
    else:
        row = pg.fetch_one(
            "SELECT MAX(trade_date) AS d FROM stock.daily WHERE trade_date <= %s",
            (on_or_before,))
    return row["d"] if row else None


def estimate_start_date(as_of: date, lookback_days: int = 260) -> date:
    """
    拉取起始日: lookback_days 个交易日按 ~1.6 倍换算为日历天并留 buffer,
    保证 NATR 分位(250D) / 边界(60D) 等长窗口指标有足够预热数据。
    """
    return as_of - timedelta(days=int(lookback_days * 1.6) + 15)


def load_universe(pg, as_of: date, min_history: int,
                  amount_floor: float, lookback: int) -> List[str]:
    """
    扫描 universe (指南"全市场 Universe"层):
    - 窗口内 (max(lookback, min_history) 个交易日) 有 >= min_history 根日K (数据充分);
    - 日均成交额 >= amount_floor (千元), 过滤流动性不足的标的。
    """
    window_trading = max(lookback, min_history)
    window_start = as_of - timedelta(days=int(window_trading * 1.6) + 15)
    rows = pg.fetch_all(
        """
        SELECT ts_code
        FROM stock.daily
        WHERE trade_date > %s AND trade_date <= %s
        GROUP BY ts_code
        HAVING COUNT(*) >= %s AND AVG(amount) >= %s
        ORDER BY ts_code
        """,
        (window_start, as_of, min_history, amount_floor),
    )
    return [r["ts_code"] for r in rows]


def load_daily_bars(pg, ts_codes: Sequence[str],
                    start: date, end: date) -> pd.DataFrame:
    """批量拉取日K并重构前复权 (长表, 含 ts_code), trade_date 转 Timestamp"""
    if not ts_codes:
        return pd.DataFrame()
    df = pg.fetch_df(_SQL_DAILY, (list(ts_codes), start, end))
    if df.empty:
        return df
    df["trade_date"] = pd.to_datetime(df["trade_date"])
    df = df.groupby("ts_code", sort=False, group_keys=False).apply(_adjust_forward)
    return df


def iter_symbol_frames(pg, ts_codes: Sequence[str],
                       start: date, end: date,
                       batch_size: int = 300) -> Iterator[Tuple[str, pd.DataFrame]]:
    """
    分批拉取并按标的展开, 逐只 yield (ts_code, df)。
    df 列: trade_date/open/high/low/close/vol/amount, 按 trade_date 升序。
    """
    codes = list(ts_codes)
    for i in range(0, len(codes), batch_size):
        batch = codes[i:i + batch_size]
        df = load_daily_bars(pg, batch, start, end)
        if df.empty:
            logger.warning(f"批次 {i // batch_size} 无数据 ({len(batch)} 只)")
            continue
        for ts_code, g in df.groupby("ts_code", sort=False):
            yield ts_code, g.reset_index(drop=True)
        logger.debug(f"已加载批次 {i // batch_size + 1}/{(len(codes) + batch_size - 1) // batch_size}")
