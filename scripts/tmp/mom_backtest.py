"""动量矛 5月全市场快速回测: 候选后20日表现"""
import pandas as pd, numpy as np
from storage.pg import PgClient
from range_trading.data.loader import load_daily_bars, load_universe
from range_trading.features.momentum import momentum_features, momentum_score, is_momentum_candidate
from datetime import date, timedelta

anchor = date(2026, 5, 6)
with PgClient() as pg:
    uni = load_universe(pg, anchor, 120, 10000.0, 60)
    print(f'universe {len(uni)}')
    ld = anchor - timedelta(days=400)
    df_all = load_daily_bars(pg, uni, ld, anchor)

cands = []
for ts, g in df_all.groupby('ts_code'):
    g = g.sort_values('trade_date').reset_index(drop=True)
    if len(g) < 130 or g['trade_date'].iloc[-1] != pd.Timestamp(anchor): continue
    try:
        s = momentum_features(g['close'], g['high'], g['low'], g['vol'])
        ms = momentum_score(s, strong_win_lo=0.10)
        if is_momentum_candidate(ms, min_score=55).iloc[-1]:
            last = ms.iloc[-1]
            cands.append({'symbol': ts, 'score': float(last['momentum_score']),
                          'ret20': float(last['ret_20']), 'state': last['momentum_state']})
    except Exception: continue
print(f'动量矛候选: {len(cands)} 只')
if not cands: raise SystemExit
cdf = pd.DataFrame(cands)

# 后续 20 日表现 (5/7 起)
with PgClient() as pg:
    fwd = load_daily_bars(pg, cdf['symbol'].tolist(), anchor, anchor + timedelta(days=45))
rows = []
for ts, g in fwd.groupby('ts_code'):
    g = g.sort_values('trade_date').reset_index(drop=True)
    if g['trade_date'].iloc[0] != pd.Timestamp(anchor): continue
    entry = g['close'].iloc[0]
    f = g.iloc[1:21]
    if len(f) < 20: continue
    rows.append({'symbol': ts, 'mfe': f['high'].max()/entry-1, 'mae': f['low'].min()/entry-1,
                 'close_ret': f['close'].iloc[-1]/entry-1})
rd = pd.DataFrame(rows)
m = cdf.merge(rd, on='symbol')
print(f'有效回测: {len(m)} 笔 (5/6 候选, 后20日)')
print(f'  胜率: {(m["close_ret"]>0).mean():.1%}')
print(f'  平均收盘: {m["close_ret"].mean()*100:.1f}%')
print(f'  平均MFE: {m["mfe"].mean()*100:.1f}%')
print(f'  主升率(MFE>8%且MAE>-4%): {((m["mfe"]>0.08)&(m["mae"]>-0.04)).mean():.1%}')
print(f'  涨40%+占比(抓大牛): {(m["close_ret"]>0.4).mean():.1%}')
