from dataclasses import replace

import numpy as np
import pandas as pd
import pytest

from skills.quant.engine import simulate
from skills.quant.models import BacktestConfig
from skills.quant.prepare import MarketSeries


def sample():
    times = pd.bdate_range("2026-01-05", periods=5) + pd.Timedelta(hours=15)
    data = {
        k: np.full(5, v)
        for k, v in dict(
            open=10.0,
            high=12.0,
            low=8.0,
            close=10.0,
            scale=1.0,
            volume=1e7,
            amount=1e8,
            up_limit=15.0,
            down_limit=5.0,
            trail_a=8.0,
            trail_b=8.0,
            atr=0.3,
        ).items()
    }
    for key in ["open", "high", "low", "close"]:
        data["adjusted_" + key] = data[key].copy()
    sig = dict(
        i=0,
        family="base_rebound",
        preferred=True,
        leg=times[0].isoformat(),
        count=1,
        stop=9.5,
        score=90.0,
    )
    s = MarketSeries("600000.SH", "样本", times[0], times, data, [sig])
    cfg = BacktestConfig(
        close_only=True, trend_exit=False, slippage=0, max_entry_risk_pct=6
    )
    return s, times, cfg


def test_intraday_extremes_do_not_imply_after_work_fills():
    s, t, cfg = sample()
    r = simulate([s], t, "swing_60f_v8", "1D", cfg=cfg)
    assert not r["trades"] and len(r["open_positions"]) == 1


@pytest.mark.parametrize("close,reason", [(9.4, "stop_loss"), (11.1, "take_profit")])
def test_close_threshold_queues_next_open_including_t1(close, reason):
    s, t, cfg = sample()
    s.data["close"][1] = s.data["adjusted_close"][1] = close
    s.data["open"][2] = s.data["adjusted_open"][2] = 9.7
    r = simulate([s], t, "swing_60f_v8", "1D", cfg=cfg)
    trade = r["trades"][0]
    assert trade["reason"] == reason and trade["exit_price"] == 9.7
    assert pd.Timestamp(trade["exit_time"]) == t[2].normalize() + pd.Timedelta(
        hours=9, minutes=30
    )
    assert r["execution_version"] == "scheduled-close-next-open-v1"


def test_scheduled_mode_rejects_intraday_clock_and_non_boolean_config():
    s, t, cfg = sample()
    with pytest.raises(ValueError):
        simulate([s], t, "swing_60f_v8", "60F", cfg=cfg)
    with pytest.raises(ValueError):
        replace(cfg, close_only="false")
