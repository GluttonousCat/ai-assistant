# -*- encoding: utf-8 -*-
"""
蓄势识别器 历史回测 (2026-01-01 起逐日)

【目标】从 2026-01-01 起, 对每个交易日扫描全市场蓄势候选 (DIGESTING),
统计候选日后 N 日的表现 (MFE/收盘涨幅/主升率/胜率), 生成逐日命中率曲线。

【口径】(事件驱动, 无前视)
  - 候选日 t: 用 base_setup_score(is_candidate) 判定 (只用 t 及之前数据);
  - 表现: t+1 起 horizon 日, MFE=最大涨幅/入场, 收盘涨幅=第N日收盘/入场-1,
          主升(surge)=MFE>=8% 且 MAE>=-4%;
  - 入场: 用候选日收盘近似 (回测口径, 真实交易需次日开盘, 此处看识别器判别力)。

【输出】output/setup_scan/daily_backtest_{start}_{end}.csv
  每行: 候选日 / 标的 / 状态 / 蓄势分 / 后续表现
"""
from __future__ import annotations

import argparse
from datetime import date, timedelta
from pathlib import Path
from typing import List, Optional

import pandas as pd

from core.logger import get_logger
from range_trading.data.loader import load_daily_bars, load_universe
from range_trading.features.base_setup import base_setup_score, is_candidate
from storage.pg import PgClient

logger = get_logger("range_trading.setup_backtest")
OUT_DIR = Path("output/setup_scan")


def trading_days(pg, start: date, end: date) -> List[date]:
    rows = pg.fetch_all(
        "SELECT cal_date FROM stock.trade_calendar WHERE is_open=1 AND cal_date BETWEEN %s AND %s ORDER BY cal_date",
        (start, end))
    return [r["cal_date"] for r in rows]


def _retry_conn(fn, retries: int = 3, wait: float = 5.0):
    """带重试的数据库操作: 网络抖动 (No route to host / connection closed) 时自动重试"""
    import time
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"DB 操作失败 (重试 {attempt + 1}/{retries}): {e}")
                time.sleep(wait * (attempt + 1))
            else:
                raise


def _with_retry(fn, retries: int = 4, wait: float = 6.0):
    """带重试的数据库操作: 网络抖动时自动重试 (No route to host / connection closed)"""
    import time
    for attempt in range(retries):
        try:
            return fn()
        except Exception as e:
            if attempt < retries - 1:
                logger.warning(f"DB 操作失败 (重试 {attempt + 1}/{retries}): {e}")
                time.sleep(wait * (attempt + 1))
            else:
                raise


def run_daily_backtest(start: date, end: date, min_score: float = 55.0,
                       max_score: float = 80.0, horizon: int = 20,
                       daily_limit: Optional[int] = None,
                       out_file: Optional[Path] = None,
                       resume: bool = False,
                       detector: str = "base") -> pd.DataFrame:
    """逐日扫描 + 后续表现统计 (带断线重试 + 增量落盘)

    detector: base=蓄势盾 / momentum=动量矛 (tech_only, 仅核心科技)
    增量落盘: 每完成一个交易日, 追加写入 out_file (若指定), 崩溃不丢进度;
    断点续跑: resume=True 时跳过 out_file 中已完成的日期。
    """
    from range_trading.features.momentum_spear import (
        momentum_features, is_momentum_candidate, industry_weight)

    # 行业映射 (动量模式 tech_only 过滤用)
    ind_map = {}
    if detector == "momentum":
        try:
            with PgClient() as pg:
                r = pg.fetch_all('''SELECT si.con_code, l1.industry_name AS l1 FROM stock.stock_industry si
                    JOIN stock.index_classify l2 ON si.index_code=l2.index_code AND l2.level='L2'
                    LEFT JOIN stock.index_classify l1 ON l2.parent_code=l1.industry_code AND l1.level='L1'
                    WHERE si.is_new='Y' ''')
            ind_map = {x['con_code']: x['l1'] for x in r}
        except Exception as e:
            logger.warning(f"行业映射加载失败: {e}")

    all_rows = []
    done_dates = set()
    if out_file and resume and out_file.exists():
        try:
            done = pd.read_csv(out_file)
            done_dates = set(done["date"].astype(str).str[:10])
            all_rows = done.to_dict(orient="records")
            logger.info(f"断点续跑: 已有 {len(done_dates)} 个交易日结果")
        except Exception:
            done_dates = set()

    def _load_days():
        with PgClient() as pg:
            return trading_days(pg, start, end)

    days = _with_retry(_load_days)
    logger.info(f"回测区间 {start} ~ {end}, {len(days)} 个交易日 (已完成 {len(done_dates)})")

    for d in days:
        d_str = d.strftime("%Y-%m-%d")
        if d_str in done_dates:
            continue
        try:
            def _load_uni():
                with PgClient() as pg:
                    return load_universe(pg, d, 120, 10000.0, 60)
            universe = _with_retry(_load_uni)
        except Exception as e:
            logger.error(f"{d} universe 拉取失败, 跳过该日: {e}")
            continue
        if daily_limit:
            universe = universe[:daily_limit]

        # 拉取预热 + 候选日行情 (分批, 每批独立连接 + 重试)
        lookback = 200
        ld = d - timedelta(days=int(lookback * 1.6))
        df_all_parts = []
        for i in range(0, len(universe), 200):
            batch = universe[i:i + 200]
            try:
                def _load_bars():
                    with PgClient() as pg:
                        return load_daily_bars(pg, batch, ld, d)
                part = _with_retry(_load_bars)
            except Exception as e:
                logger.warning(f"{d} 批 {i // 200} 拉取失败跳过: {e}")
                continue
            if not part.empty:
                df_all_parts.append(part)
        if not df_all_parts:
            continue
        df_all = pd.concat(df_all_parts, ignore_index=True)

        cands = []
        for ts_code, g in df_all.groupby("ts_code", sort=False):
            g = g.sort_values("trade_date").reset_index(drop=True)
            if len(g) < 130 or g["trade_date"].iloc[-1] != pd.Timestamp(d):
                continue
            if detector == "momentum":
                # 动量矛: 横盘不跌 + 量能不缩 + tech_only
                try:
                    m = momentum_features(g["close"], g["high"], g["low"], g["vol"])
                    m["l1"] = ind_map.get(ts_code, "")
                except Exception:
                    continue
                if not is_momentum_candidate(m, 60.0, tech_only=True).iloc[-1]:
                    continue
                ml = m.iloc[-1]
                cands.append({"symbol": ts_code, "base_score": float(ml["momentum_score"]),
                              "limit_up": bool(ml["limit5"] > 0),
                              "rally_vol": float(ml["vol_ratio"]),
                              "vol_shrink": float(ml["dd7"]),
                              "cum_rally": float(ml["rally20"]),
                              "close": float(g["close"].iloc[-1])})
            else:
                try:
                    s = base_setup_score(g["close"], g["high"], g["low"], g["vol"])
                except Exception:
                    continue
                if is_candidate(s, min_score, max_score).iloc[-1]:
                    last = s.iloc[-1]
                    cands.append({"symbol": ts_code, "base_score": float(last["base_score"]),
                                  "limit_up": bool(last["limit_up_any"]),
                                  "rally_vol": float(last["rally_vol"]),
                                  "vol_shrink": float(last["vol_shrink"]),
                                  "cum_rally": float(last["cum_rally"]),
                                  "close": float(g["close"].iloc[-1])})
        if not cands:
            continue

        # 拉候选后续行情 (分批独立连接 + 重试)
        codes = [c["symbol"] for c in cands]
        fwd_rows = []
        for i in range(0, len(codes), 150):
            batch = codes[i:i + 150]
            try:
                def _load_fwd():
                    with PgClient() as pg:
                        return load_daily_bars(pg, batch, d, d + timedelta(days=int(horizon * 2) + 5))
                df_fwd = _with_retry(_load_fwd)
            except Exception as e:
                logger.warning(f"{d} 批 {i // 150} 后续拉取失败: {e}")
                continue
            for ts, g in df_fwd.groupby("ts_code", sort=False):
                g = g.sort_values("trade_date").reset_index(drop=True)
                if g.empty or g["trade_date"].iloc[0] != pd.Timestamp(d):
                    continue
                entry = g["close"].iloc[0]
                fwd = g.iloc[1: horizon + 1]
                if len(fwd) < horizon:
                    continue
                mfe = fwd["high"].max() / entry - 1.0
                mae = fwd["low"].min() / entry - 1.0
                close_ret = fwd["close"].iloc[-1] / entry - 1.0
                surge = bool(mfe >= 0.08 and mae >= -0.04)
                fwd_rows.append({"mfe": float(mfe), "mae": float(mae),
                                 "close_ret": float(close_ret), "surge": surge})

        for c, f in zip(cands, fwd_rows):
            all_rows.append({"date": d, **c, **f})
        logger.info(f"{d}: 候选 {len(cands)} 只 (累计 {len(all_rows)})")

        # 增量落盘: 每完成一天写一次, 崩溃不丢进度
        if out_file:
            pd.DataFrame(all_rows).to_csv(out_file, index=False, encoding="utf-8-sig")

    return pd.DataFrame(all_rows)



def summarize_daily(bt: pd.DataFrame) -> pd.DataFrame:
    """按候选日汇总: 候选数 / 胜率 / 主升率 / 平均涨幅"""
    if bt.empty:
        return pd.DataFrame()
    rows = []
    for d, g in bt.groupby("date"):
        # date 可能是 date 对象或字符串 (CSV 读回)
        d_str = d.strftime("%Y-%m-%d") if hasattr(d, "strftime") else str(d)[:10]
        rows.append({
            "date": d_str,
            "n": len(g),
            "win_rate": round((g["close_ret"] > 0).mean(), 4),
            "surge_rate": round(g["surge"].mean(), 4),
            "avg_mfe": round(g["mfe"].mean(), 4),
            "avg_close_ret": round(g["close_ret"].mean(), 4),
            "limit_up_n": int(g["limit_up"].sum()),
            "avg_score": round(g["base_score"].mean(), 1),
        })
    return pd.DataFrame(rows)


def main():
    parser = argparse.ArgumentParser(description="蓄势识别器逐日历史回测")
    parser.add_argument("--start", type=str, default="2026-01-01")
    parser.add_argument("--end", type=str, default=None)
    parser.add_argument("--horizon", type=int, default=20)
    parser.add_argument("--min-score", type=float, default=55.0)
    parser.add_argument("--max-score", type=float, default=80.0)
    parser.add_argument("--limit", type=int, default=None, help="每日 universe 上限 (调试)")
    parser.add_argument("--resume", action="store_true", help="断点续跑: 跳过已落盘日期")
    parser.add_argument("--detector", type=str, default="base", choices=["base", "momentum"],
                        help="识别器: base=蓄势盾 / momentum=动量矛(tech_only)")
    args = parser.parse_args()

    start = pd.Timestamp(args.start).date()
    end = pd.Timestamp(args.end).date() if args.end else date.today()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    tag = f"{start.strftime('%Y%m%d')}_{end.strftime('%Y%m%d')}"
    prefix = "momentum_backtest" if args.detector == "momentum" else "daily_backtest"
    out_f = OUT_DIR / f"{prefix}_{tag}.csv"

    bt = run_daily_backtest(start, end, args.min_score, args.max_score,
                            args.horizon, args.limit, out_f, args.resume,
                            detector=args.detector)
    if bt.empty:
        print("无结果")
        return
    bt.to_csv(out_f, index=False, encoding="utf-8-sig")
    daily = summarize_daily(bt)
    daily.to_csv(OUT_DIR / f"{prefix}_{tag}_daily.csv", index=False, encoding="utf-8-sig")

    print("=" * 70)
    print(f"        {args.detector} 识别器逐日回测 ({start} ~ {end}, horizon={args.horizon}日)")
    print("=" * 70)
    print(f"总候选: {len(bt)} 笔, 覆盖 {len(daily)} 个交易日")
    print(f"整体胜率(ret>0): {(bt['close_ret']>0).mean():.1%}")
    print(f"整体主升率(surge): {bt['surge'].mean():.1%}")
    print(f"平均MFE: {bt['mfe'].mean():.1%}  平均收盘: {bt['close_ret'].mean():.1%}")
    print(f"涨停启动占比: {bt['limit_up'].mean():.1%}")
    print("")
    print("最近 15 个交易日的逐日表现:")
    print(daily.tail(15).to_string(index=False))



if __name__ == "__main__":
    main()