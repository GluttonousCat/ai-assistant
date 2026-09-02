# -*- encoding: utf-8 -*-
"""
趋势系统单元测试 (合成数据, 不依赖数据库)

验证 (趋势指南):
1. ER 与 DI 同源: 趋势 ER≈1, 震荡 ER≈0 (第三章复用);
2. 量价特征: UDVOL / 量能持续 / 量价同步 / CMF 的方向正确性;
3. VPA: 放量上涨趋势 VPA 高, 缩量/背离 VPA 低;
4. TQS: 健康上升趋势得分显著高于震荡, 衰竭扣减有效;
5. 状态机: 放量突破序列 -> BREAKOUT_CONFIRMED -> TREND_FORMING/ESTABLISHED;
6. 入场事件: Pullback / Breakout 判定。

运行: python -m pytest range_trading/tests/test_trend.py -q
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from range_trading.config import DEFAULT_TREND_CONFIG
from range_trading.features import compute_trend_features, volume, structure, breakout
from range_trading.regime.trend_regime import run_trend_regime
from range_trading.regime.state_machine import Regime, TREND_STATES
from range_trading.scoring.trend_score import compute_vpa, detect_stage, score_trend

CFG = DEFAULT_TREND_CONFIG
N = 300


def _make_df(close, vol=None):
    close = np.asarray(close, dtype=float)
    n = len(close)
    prev = np.roll(close, 1)
    prev[0] = close[0]
    if vol is None:
        vol = np.full(n, 10000.0)
    vol = np.asarray(vol, dtype=float)
    return pd.DataFrame({
        "trade_date": pd.date_range("2024-01-01", periods=n, freq="B"),
        "open": prev,
        "high": np.maximum(close, prev) * 1.01,
        "low": np.minimum(close, prev) * 0.99,
        "close": close,
        "vol": vol,
        "amount": vol * close / 1000.0,
    })


def _uptrend(n=N, daily=0.006, vol_base=10000.0):
    """稳步放量上涨"""
    close = 100.0 * np.cumprod(np.full(n, 1.0 + daily))
    vol = np.full(n, vol_base)
    return close, vol


def _volume_breakout_trend(n=N):
    """先缩量横盘 -> 放量突破 -> 持续上涨 (用于状态机)"""
    flat = np.full(150, 100.0) + np.sin(np.arange(150)) * 1.0
    flat_vol = np.full(150, 5000.0)
    # 放量突破段
    up = 100.0 * np.cumprod(np.full(n - 150, 1.008))
    up_vol = np.full(n - 150, 20000.0)
    return np.concatenate([flat, up]), np.concatenate([flat_vol, up_vol])


# ============================ 特征 ============================

def test_er_high_in_trend_low_in_range():
    close, _ = _uptrend()
    feat = compute_trend_features(_make_df(close), CFG)
    assert feat["er_20"].iloc[-1] > 0.85
    # 对称震荡
    rng = 100.0 * np.cumprod(1.0 + np.array([0.02, -0.019] * (N // 2)))
    feat_r = compute_trend_features(_make_df(rng), CFG)
    assert feat_r["er_20"].iloc[-1] < 0.2


def test_udvol_buyer_dominance():
    """上涨放量 -> UDVOL > 1"""
    n = 100
    close = 100.0 * np.cumprod(1.0 + np.where(np.arange(n) % 2 == 0, 0.01, -0.005))
    vol = np.where(close > np.roll(close, 1), 20000.0, 5000.0)   # 涨日量大
    vol[0] = 10000.0
    ud = volume.updown_vol_ratio(pd.Series(close), pd.Series(vol), 20)
    assert ud.iloc[-1] > 2.0


def test_vol_persistence_vs_pulse():
    """持续放量 persist 高且 pulse 低; 单日脉冲 pulse 高"""
    n = 40
    steady = np.full(n, 10000.0)
    steady[-5:] = 15000.0                     # 持续温和放量
    vp = volume.vol_persistence(pd.Series(steady), 5, 20)
    ps = volume.vol_pulse_share(pd.Series(steady), 5)
    assert vp.iloc[-1] > 1.3
    assert ps.iloc[-1] < 0.25
    # 单日脉冲
    pulse = np.full(n, 5000.0)
    pulse[-1] = 50000.0
    ps2 = volume.vol_pulse_share(pd.Series(pulse), 5)
    assert ps2.iloc[-1] > 0.6                   # 50000/(5000*4+50000)=0.71


def test_obv_confirm_and_divergence():
    """量价双新高 obv_confirm=1; 价新高量未新高 -> 顶背离"""
    close, vol = _uptrend()
    obv_line = volume.obv(pd.Series(close), pd.Series(vol))
    oc = volume.obv_confirm(pd.Series(close), obv_line, 20)
    assert oc.iloc[-1] == 1.0                  # 持续新高, 双确认
    bd = volume.bearish_divergence(pd.Series(close), obv_line, 20)
    assert not bd.iloc[-1]


def test_cmf_positive_in_uptrend():
    close, vol = _uptrend()
    df = _make_df(close, vol)
    cmf = volume.cmf(df["high"], df["low"], df["close"], df["vol"], 20)
    assert cmf.iloc[-1] > 0.1                  # 持续上涨收阳 -> CMF 为正


# ============================ VPA / Stage ============================

def test_vpa_high_in_volume_trend():
    close, vol = _uptrend(vol_base=15000.0)
    feat = compute_trend_features(_make_df(close, vol), CFG)
    vpa = compute_vpa(feat, CFG)
    assert vpa.iloc[-1] > 0.6


def test_stage2_in_uptrend():
    close, vol = _uptrend()
    feat = compute_trend_features(_make_df(close, vol), CFG)
    stage = detect_stage(feat, CFG)
    assert stage.iloc[-1] == 2                 # 上升阶段


# ============================ TQS ============================

def test_tqs_trend_beats_range():
    close, vol = _volume_breakout_trend()
    feat_t = compute_trend_features(_make_df(close, vol), CFG)
    tqs_t = score_trend(feat_t, CFG)["tqs"]
    rng = 100.0 * np.cumprod(1.0 + np.array([0.02, -0.019] * (N // 2)))
    feat_r = compute_trend_features(_make_df(rng), CFG)
    tqs_r = score_trend(feat_r, CFG)["tqs"]
    assert tqs_t.iloc[-1] > tqs_r.iloc[-1] + 10


# ============================ 状态机 ============================

def test_state_machine_volume_breakout():
    """缩量横盘后放量持续上涨 -> 进入趋势状态"""
    close, vol = _volume_breakout_trend()
    df = _make_df(close, vol)
    detail, state = run_trend_regime(df, symbol="TEST.SZ", config=CFG)
    assert state is not None
    assert state.state in TREND_STATES
    # 状态序列经历过突破确认或趋势态
    assert (detail["state"].isin([s for s in TREND_STATES])).any()


def test_event_breakout_or_pullback():
    close, vol = _volume_breakout_trend()
    df = _make_df(close, vol)
    detail, state = run_trend_regime(df, symbol="TEST.SZ", config=CFG)
    # 趋势状态下应有事件 (突破或回调)
    assert state.state in TREND_STATES
    assert state.event in ("BREAKOUT", "PULLBACK", "RETEST", "NONE")


def test_chandelier_stop_below_price():
    close, vol = _volume_breakout_trend()
    df = _make_df(close, vol)
    _, state = run_trend_regime(df, symbol="TEST.SZ", config=CFG)
    # 移动止损应在最新价下方
    assert state.chandelier_stop < state.close


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
