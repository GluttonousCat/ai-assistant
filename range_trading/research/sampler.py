"""
校验抽样设计 (2024-01 ~ 2026-08)

【数据约束】stock.daily 在 2019-03 ~ 2024-01 存在整段空洞 (pct_chg 重构复权同源),
因此校验窗口只能用 2024-01-25 之后的连续数据。所幸该窗口完整覆盖一个牛熊轮回:
    2024-02 微盘崩盘 -> 2024-09 924暴涨 -> 2025-04 关税冲击 -> 2025 慢牛 -> 2026。

为在单窗口内保证样本丰富:
- 每个市场状态段取多个 anchor 交易日 (等距 4 个), 共 6 段 x 4 = 24 个锚点;
- 每个锚点按板块前缀分层随机抽样 (~120 只), 全样本 ~24 x 120 = 2880 个信号点;
- 锚点市场状态用当日横截面统计客观标注 (中位涨跌幅 / 上涨占比 / 近20日波动),
  不依赖外部指数。

可复现: 固定随机种子。
"""
from __future__ import annotations

import random
from datetime import date, timedelta
from typing import Dict, List

import numpy as np

from storage.pg import PgClient

# (段标签, 起始, 结束, 市场状态备注)
# 约束: stock.daily 2024-01-25 才有连续数据, min_history=150 -> 锚点须 >= ~2024-12
# 该窗口完整覆盖: 2024末高位宽幅 -> 2025关税冲击 -> 2025慢牛 -> 2026高位
REGIME_SEGMENTS: List[tuple] = [
    ("late2024",   date(2024, 12, 2),  date(2024, 12, 31), "924后高位宽幅震荡"),
    ("tariff_2025", date(2025, 3, 1),  date(2025, 5, 30),  "关税冲击/急跌后修复"),
    ("grindup_2025", date(2025, 7, 1), date(2025, 11, 28), "慢牛爬升"),
    ("year2026",   date(2026, 1, 5),   date(2026, 6, 30),  "2026高位回落震荡"),
]

ANCHORS_PER_SEGMENT = 6

PREFIX_GROUPS = {
    "sh_main": ["600", "601", "603", "605"],
    "sz_main": ["000", "001", "002", "003"],
    "cyb": ["300", "301"],
    "star": ["688"],
}


def trading_days_between(pg, start: date, end: date) -> List[date]:
    rows = pg.fetch_all(
        "SELECT DISTINCT trade_date FROM stock.daily "
        "WHERE trade_date BETWEEN %s AND %s ORDER BY trade_date", (start, end))
    return [r["trade_date"] for r in rows]


def build_anchor_dates(pg) -> List[Dict]:
    """每个市场状态段等距取 ANCHORS_PER_SEGMENT 个 anchor 交易日"""
    out: List[Dict] = []
    for tag, start, end, note in REGIME_SEGMENTS:
        days = trading_days_between(pg, start, end)
        if not days:
            continue
        # 等距取 anchor (含首尾, 但跳过最后 30 天以免前瞻窗口不足)
        usable = days[:-25] if len(days) > 30 else days
        idxs = np.linspace(0, len(usable) - 1, ANCHORS_PER_SEGMENT).astype(int)
        for i in sorted(set(int(x) for x in idxs)):
            out.append({"tag": tag, "anchor": usable[i], "regime_note": note})
    return out


def market_regime_snapshot(pg, anchor: date, lookback: int = 20) -> Dict:
    """锚点市场状态: 当日横截面 + 近 lookback 日累积表现"""
    row = pg.fetch_one(
        """
        SELECT ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY pct_chg)::numeric,3) med_chg,
               ROUND(AVG(CASE WHEN pct_chg>0 THEN 1.0 ELSE 0 END)*100,1) up_pct,
               ROUND(PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ABS(pct_chg))::numeric,3) med_abs
        FROM stock.daily WHERE trade_date = %s
        """, (anchor,))
    # 近 lookback 日累计中位收益
    row2 = pg.fetch_one(
        """
        SELECT ROUND(AVG(cum)*100,2) avg_cum FROM (
          SELECT (EXP(SUM(LN(GREATEST(1+pct_chg/100.0,0.01))))-1) cum
          FROM stock.daily
          WHERE trade_date > %s AND trade_date <= %s
          GROUP BY ts_code HAVING COUNT(*) >= %s
        ) t
        """,
        (anchor - timedelta(days=lookback * 2), anchor, lookback // 2))
    return {
        "med_chg": float(row["med_chg"]) if row and row["med_chg"] is not None else np.nan,
        "up_pct": float(row["up_pct"]) if row and row["up_pct"] is not None else np.nan,
        "med_abs": float(row["med_abs"]) if row and row["med_abs"] is not None else np.nan,
        "cum20_pct": float(row2["avg_cum"]) if row2 and row2["avg_cum"] is not None else np.nan,
    }


def eligible_universe(pg, anchor: date, min_history: int = 150,
                      min_amount: float = 5000.0) -> List[str]:
    """锚点可交易 universe (自适应流动性下限, 以该时点横截面 35 分位为底)
    历史窗口锚定数据起点 (2024-01-25), 不超过可用区间。"""
    start = anchor - timedelta(days=int(min_history * 1.7))
    if start < date(2024, 1, 24):
        start = date(2024, 1, 24)
    rows = pg.fetch_all(
        """
        SELECT ts_code, AVG(amount) avg_amt FROM stock.daily
        WHERE trade_date > %s AND trade_date <= %s
        GROUP BY ts_code HAVING COUNT(*) >= %s
        """, (start, anchor, min_history))
    if not rows:
        return []
    amts = np.array([float(r["avg_amt"]) for r in rows])
    floor = max(min_amount, float(np.percentile(amts, 35)))
    return sorted(r["ts_code"] for r in rows if float(r["avg_amt"]) >= floor)


def stratified_sample(codes: List[str], per_group: int, seed: int = 42) -> List[str]:
    """按板块前缀分层随机抽样 (跨 anchor 复用同一种子 -> 同一标的池, 便于纵向观察)"""
    rnd = random.Random(seed)
    picked: List[str] = []
    for prefixes in PREFIX_GROUPS.values():
        pool = [c for c in codes if any(c.startswith(p) for p in prefixes)]
        rnd.shuffle(pool)
        picked.extend(pool[:per_group])
    return sorted(set(picked))
