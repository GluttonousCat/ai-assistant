"""
range_trading 单元测试 (合成数据, 不依赖数据库)

验证:
1. 核心指标的数学正确性 (DI / FlipRate / AC1 / 分位数 / 斜率 / HH率);
2. rolling_percentile 与边界反应的无前视性质;
3. 打分: 震荡段得分显著高于趋势段, 趋势延续证据会扣减分数;
4. 状态机: 趋势 -> 震荡的合成序列最终进入 RANGE 族状态。

运行: python -m pytest range_trading/tests -q
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from range_trading.config import DEFAULT_DAILY_CONFIG
from range_trading.features import (
    boundary,
    compute_daily_features,
    directional,
    structure,
    volatility,
)
from range_trading.features.candle import false_breakout
from range_trading.features import volume as volume_feat
from range_trading.regime.daily_regime import run_daily_regime
from range_trading.regime.state_machine import RANGE_STATES, Regime
from range_trading.scoring.daily_score import score_daily

CFG = DEFAULT_DAILY_CONFIG
N = 300


def _make_df(close: np.ndarray, vol: np.ndarray | None = None) -> pd.DataFrame:
    """由收盘序列构造 OHLCV (open=昨收, high/low=收盘±1%); vol 可选注入量能"""
    close = np.asarray(close, dtype=float)
    if vol is None:
        vol = np.full(len(close), 10000.0)
    prev = np.roll(close, 1)
    prev[0] = close[0]
    return pd.DataFrame({
        "trade_date": pd.date_range("2024-01-01", periods=len(close), freq="B"),
        "open": prev,
        "high": np.maximum(close, prev) * 1.01,
        "low": np.minimum(close, prev) * 0.99,
        "close": close,
        "vol": np.asarray(vol, dtype=float),
        "amount": 100000.0,
    })


def _trend_series(n: int = N, start: float = 100.0, daily: float = 0.004) -> np.ndarray:
    return start * np.cumprod(np.full(n, 1.0 + daily))


def _range_series(n: int = N, start: float = 100.0,
                  up: float = 0.02, down: float = -0.019) -> np.ndarray:
    rets = np.array([up, down] * (n // 2 + 1))[:n]
    return start * np.cumprod(1.0 + rets)


# ============================ Directional ============================

def test_di_trend_near_zero():
    """完美趋势: ER≈1 -> DI≈0 (指南第三节示例)"""
    close = pd.Series(_trend_series())
    di = directional.directional_inefficiency(close, 20)
    assert di.iloc[-1] < 0.05


def test_di_range_near_one():
    """交替震荡: 路径长 / 净位移小 -> DI≈1"""
    close = pd.Series(_range_series())
    di = directional.directional_inefficiency(close, 20)
    assert di.iloc[-1] > 0.95


def test_di_slope_and_acceleration():
    close = pd.Series(_range_series())
    di = directional.directional_inefficiency(close, 20)
    slope = directional.di_slope(di, 5)
    accel = directional.di_acceleration(di, 5)
    # 稳定震荡中 DI 近似恒定 -> 斜率/加速度接近 0 (乘法累积存在微小漂移)
    assert abs(slope.iloc[-1]) < 5e-3
    assert abs(accel.iloc[-1]) < 5e-3
    # 长度与 index 对齐, 前导为 NaN
    assert slope.index.equals(di.index)
    assert np.isnan(slope.iloc[4])


def test_flip_rate_extremes():
    """趋势 FlipRate≈0; 交替序列 FlipRate≈1 (指南第六节)"""
    trend = directional.flip_rate(pd.Series(_trend_series()), 20)
    rng = directional.flip_rate(pd.Series(_range_series()), 20)
    assert trend.iloc[-1] < 0.1
    assert rng.iloc[-1] > 0.9


def test_ac1_negative_in_range():
    """震荡中 r_t 与 r_{t-1} 负相关 -> AC1 < 0 (指南第七节)"""
    ac1 = directional.autocorr1(pd.Series(_range_series()), 20)
    assert ac1.iloc[-1] < -0.9


# ============================ Volatility ============================

def test_rolling_percentile_known_values():
    s = pd.Series([1.0, 2.0, 3.0, 2.0, 1.0])
    pct = volatility.rolling_percentile(s, 3)
    assert np.isnan(pct.iloc[0]) and np.isnan(pct.iloc[1])
    assert pct.iloc[2] == pytest.approx(1.0)          # [1,2,3] 3 最大
    assert pct.iloc[3] == pytest.approx(2.0 / 3.0)    # [2,3,2] last=2, <= 计 2 个
    assert pct.iloc[4] == pytest.approx(1.0 / 3.0)    # [3,2,1] 1 最小


def test_rolling_percentile_short_history_fallback():
    """历史不足 window 时退化为 expanding 分位数, 不应全 NaN"""
    s = pd.Series(np.arange(1.0, 31.0))               # 30 个递增值, window=250
    pct = volatility.rolling_percentile(s, 250)
    assert not pct.iloc[5:].isna().any()
    assert pct.iloc[-1] == pytest.approx(1.0)         # 单调递增 -> 恒为最大


def test_natr_positive_and_percentile_range():
    df = _make_df(_range_series())
    natr = volatility.natr(df["high"], df["low"], df["close"], 14)
    assert (natr.dropna() > 0).all()
    pct = volatility.rolling_percentile(natr, 60)
    valid = pct.dropna()
    assert ((valid >= 0) & (valid <= 1)).all()


# ============================ Boundary ============================

def test_quantile_boundary_ignores_spikes():
    """异常 spike 不污染分位数边界 (指南第九节: 不用 rolling max/min)"""
    n = 60
    close = pd.Series(np.full(n, 100.0) + np.sin(np.arange(n)) * 2.0)
    close.iloc[-1] = 200.0                       # 末日 spike
    upper, lower = boundary.quantile_boundary(close, 30, 0.9, 0.1)
    assert upper.iloc[-1] < 110                  # Q90 仍接近震荡区上沿
    assert lower.iloc[-1] > 90


def test_range_pos_definition():
    upper = pd.Series([110.0, 110.0])
    lower = pd.Series([90.0, 90.0])
    pos = boundary.range_pos(pd.Series([100.0, 115.0]), upper, lower)
    assert pos.iloc[0] == pytest.approx(0.5)
    assert pos.iloc[1] > 1.0                     # 越界不裁剪, 携带突破信息


def test_boundary_rejection_no_lookahead():
    """触碰反应 MFE 只在 horizon 天后可见 (无前视)"""
    n = 8
    high = pd.Series([10.0] * n)
    low = pd.Series([9.9, 9.9, 9.9, 9.0, 9.0, 9.9, 9.9, 9.9])  # t=3,4 回落
    close = pd.Series([10.0] * n)
    atr_s = pd.Series([1.0] * n)
    upper = pd.Series([10.5] * n)
    lower = pd.Series([9.5] * n)
    # band=0.1 -> 触碰带 0.1: t=2 的 high=10 < 10.4 不触上沿; 用 high[2]=12 构造触碰
    high.iloc[2] = 12.0
    _, res_resp = boundary.boundary_rejection(
        high, low, close, atr_s, upper, lower, band=0.1, horizon=3, window=10)
    # t=2 触碰上沿, 反应 = (10 - min(low[3..5]))/ATR = (10-9)/1 = 1.0, t=5 才可知
    assert np.isnan(res_resp.iloc[4])
    assert res_resp.iloc[5] == pytest.approx(1.0)
    assert res_resp.iloc[7] == pytest.approx(1.0)


def test_false_breakout_definition():
    high = pd.Series([11.0, 10.5])
    close = pd.Series([10.6, 10.6])
    upper = pd.Series([10.7, 10.7])
    fb = false_breakout(high, close, upper)
    assert fb.iloc[0]           # High>Upper 且 Close<Upper
    assert not fb.iloc[1]       # High 未破 Upper


# ============================ Structure ============================

def test_ema_slope_and_hh_hl_rates():
    df = _make_df(_trend_series())
    slope = structure.ema_slope(df["close"], 20, 10)
    assert 0.002 < slope.iloc[-1] < 0.006        # 每日 ~0.4%
    hh = structure.higher_high_rate(df["high"], 20)
    assert hh.iloc[-1] == pytest.approx(1.0)     # 单调趋势 HH 率 = 1

    rng = _make_df(_range_series())
    hh_r = structure.higher_high_rate(rng["high"], 20)
    assert hh_r.iloc[-1] == pytest.approx(0.5)   # 交替序列 HH 率 = 0.5

    net_trend = structure.net_slope(df["close"], 60)
    net_rng = structure.net_slope(rng["close"], 60)
    assert net_trend.iloc[-1] > 0.003
    assert abs(net_rng.iloc[-1]) < 0.001         # 震荡净位移 ≈ 0


# ============================ Scoring ============================

def _score_of(close: np.ndarray) -> pd.Series:
    df = _make_df(close)
    feat = compute_daily_features(df, CFG)
    return score_daily(feat, CFG)["daily_range_score"]


def test_range_scores_higher_than_trend():
    """震荡段 DailyRangeScore 显著高于趋势段"""
    s_range = _score_of(_range_series())
    s_trend = _score_of(_trend_series())
    assert s_range.iloc[-1] > 55
    assert s_trend.iloc[-1] < 30
    assert s_range.iloc[-1] - s_trend.iloc[-1] > 25


def test_trend_continuation_penalty():
    """趋势延续证据 (HH/HL + 净位移 + DI_long 低) 应扣减分数"""
    df = _make_df(_trend_series())
    feat = compute_daily_features(df, CFG)
    scored = score_daily(feat, CFG)
    assert scored["trend_cont"].iloc[-1] > 90
    # 高波动趋势 (指南第三十九节): 大幅锯齿但净位移持续向上 (+5%/-3% 交替)
    n = 300
    rets = np.where(np.arange(n) % 2 == 0, 0.05, -0.03)
    close = 100.0 * np.cumprod(1.0 + rets)
    s_hv = _score_of(close)
    # 同等波动幅度的对称震荡 (每对收益乘积=1, 无净位移) 作对照
    sym = np.array([0.05, -1.0 + 1.0 / 1.05] * (n // 2))[:n]
    close_sym = 100.0 * np.cumprod(1.0 + sym)
    s_sym = _score_of(close_sym)
    assert s_hv.iloc[-1] < 45                        # 被净位移证据压低
    assert s_sym.iloc[-1] > s_hv.iloc[-1] + 15       # 对称震荡分数显著更高


def test_wide_range_penalized():
    """区间宽度过宽 (暴涨后横住, 30D 分位区间 > 60%) 应被扣减 (指南第三十四节)"""
    # 先翻倍式上涨制造宽分布, 再小幅交替横盘 -> 窗口内 quantile 宽度极大
    leg = _trend_series(60, daily=0.012)
    flat = np.concatenate([leg, np.repeat(leg[-1], 240)])
    s = _score_of(flat)
    assert s.iloc[-1] < 55                       # 超宽区间被 width_penalty 压低


# ============================ Regime / 状态机 ============================

def test_state_machine_trend_to_range():
    """前半趋势 + 后半震荡 -> 最终进入 RANGE 族 (TREND->...->RANGE transition)"""
    trend = _trend_series(320)      # 加长以越过 NATR 分位 (250D) 预热期
    rng = _range_series(300, start=trend[-1])
    df = _make_df(np.concatenate([trend, rng]))
    detail, state = run_daily_regime(df, symbol="TEST.SZ", config=CFG)

    assert state is not None
    assert state.state in RANGE_STATES
    assert state.state in {Regime.RANGE_FORMATION.value,
                           Regime.EARLY_TRADABLE_RANGE.value,
                           Regime.MATURE_RANGE.value,
                           Regime.RANGE_RENEWAL.value}
    assert state.age is not None and state.age >= 0
    # 震荡段的分数均值应高于趋势段 (取 NATR 分位预热完成后的区间)
    mid = 320
    tail_scores = detail["daily_range_score"].iloc[mid + 100:].dropna()
    head_scores = detail["daily_range_score"].iloc[mid - 60:mid].dropna()
    assert len(head_scores) > 0
    assert tail_scores.mean() > head_scores.mean() + 25
    # 状态序列确实经历过 TREND 阶段
    assert (detail["state"].iloc[:mid] == Regime.TREND.value).any()


def test_state_machine_detects_breakout():
    """成熟震荡后放量持续上破 -> 先进入 BREAKOUT, 确立后回到 TREND"""
    rng = _range_series(300, start=100.0)
    upleg = 100.0 * 1.02
    breakout = upleg * np.cumprod(np.full(30, 1.01))       # 连续上行 30 天
    breakout = breakout * (rng[-1] / upleg)
    close = np.concatenate([rng, breakout])
    vol = np.concatenate([np.full(300, 10000.0), np.full(30, 20000.0)])  # 突破段放量
    df = _make_df(close, vol=vol)
    detail, state = run_daily_regime(df, symbol="TEST.SZ", config=CFG)
    states_tail = detail["state"].iloc[300:]
    assert (states_tail == Regime.BREAKOUT.value).any()    # 放量突破被确认
    # 突破确立后不再处于 RANGE 族
    assert state.state not in RANGE_STATES


def test_state_machine_low_volume_breakout_not_confirmed():
    """缩量突破不确认 BREAKOUT (量能确认, 指南第四十节)"""
    rng = _range_series(300, start=100.0)
    upleg = 100.0 * 1.02
    breakout = upleg * np.cumprod(np.full(30, 1.01))
    breakout = breakout * (rng[-1] / upleg)
    close = np.concatenate([rng, breakout])
    vol = np.concatenate([np.full(300, 10000.0), np.full(30, 5000.0)])   # 突破段缩量
    df = _make_df(close, vol=vol)
    detail, state = run_daily_regime(df, symbol="TEST.SZ", config=CFG)
    assert not (detail["state"].iloc[300:] == Regime.BREAKOUT.value).any()
    assert state.state != Regime.BREAKOUT.value


# ============================ Volume ============================

def test_volume_ratio():
    vol = pd.Series(np.concatenate([np.full(20, 100.0), [200.0]]))
    ratio = volume_feat.volume_ratio(vol, 20)
    assert ratio.iloc[-1] == pytest.approx(200.0 / 105.0)
    assert np.isnan(ratio.iloc[18])                        # 窗口未满


def test_obv_slope_direction():
    """上涨日放量 / 下跌日缩量 -> OBV 斜率为正 (吸筹特征)"""
    close = pd.Series(_range_series(60))
    n = len(close)
    up_days = (close.diff() > 0).to_numpy()
    vol = pd.Series(np.where(up_days, 2000.0, 1000.0))
    slope = volume_feat.obv_slope(close, vol, 20, 20)
    assert slope.iloc[-1] == pytest.approx((10 * 2000 - 10 * 1000) / (10 * 2000 + 10 * 1000))


def test_vwap_dev_lean():
    """上涨日放量 -> 量能重心 (VWAP) 偏区间上部 -> vwap_dev > 0"""
    close = _range_series(120)
    up_days = np.roll(close, 1) < close
    vol = np.where(up_days, 3000.0, 1000.0)
    df = _make_df(close, vol=vol)
    feat = compute_daily_features(df, CFG)
    assert feat["vwap_dev"].iloc[-1] > 0.0
    assert -1.5 < feat["vwap_dev"].iloc[-1] < 1.5


def test_boundary_rejection_volume_weighted():
    """放量触碰的反应权重更高: 加权均值偏向放量的那次触碰"""
    n = 10
    close = pd.Series([10.0] * n)
    low = pd.Series([9.9] * n); low.iloc[2] = 9.4; low.iloc[6] = 9.4   # 两次下沿触碰
    high = pd.Series([10.1] * n); high.iloc[3:6] = 11.0; high.iloc[8] = 13.0
    atr_s = pd.Series([1.0] * n)
    upper = pd.Series([10.5] * n)
    lower = pd.Series([9.5] * n)
    vr = pd.Series([1.0] * n); vr.iloc[2] = 3.0                        # t=2 放量

    sup_w, _ = boundary.boundary_rejection(
        high, low, close, atr_s, upper, lower, band=0.1, horizon=3, window=10,
        volume_ratio=vr)
    sup_eq, _ = boundary.boundary_rejection(
        high, low, close, atr_s, upper, lower, band=0.1, horizon=3, window=10)
    # t=2 反应 1.0 ATR (w=3), t=6 反应 3.0 ATR (w=1), 均成熟于 horizon 后
    assert sup_eq.iloc[9] == pytest.approx(2.0)            # 等权 (1+3)/2
    assert sup_w.iloc[9] == pytest.approx(1.5)             # 加权 (1*3+3*1)/4
    assert np.isnan(sup_w.iloc[4])                         # 第一次反应 t=5 才可知
    assert sup_w.iloc[8] == pytest.approx(1.0)             # t=8 仅含第一次反应


def test_range_state_snapshot_fields():
    """RangeState 输出第十七章要求的核心字段"""
    df = _make_df(_range_series())
    _, state = run_daily_regime(df, symbol="TEST.SZ", config=CFG)
    d = state.to_dict()
    for key in ("symbol", "state", "upper", "lower", "range_score", "age",
                "support_response", "resistance_response", "trend_risk",
                "long_bias", "short_bias", "range_compat", "range_prob"):
        assert key in d
    assert state.lower < state.close < state.upper or state.state in RANGE_STATES


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
