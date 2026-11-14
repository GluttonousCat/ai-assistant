import numpy as np
import pandas as pd
import pytest

from jobs.research_mainboard_60f import preregister
from skills.quant.prepare import MarketSeries, prepare_swing
from skills.trend.tests.test_swing import bars


def test_research_declaration_round_trips_and_rejects_changed_source(tmp_path):
    source = tmp_path / "source.pkl"
    source.write_bytes(b"trusted test fixture")
    first = preregister(tmp_path, source)
    assert preregister(tmp_path, source) == first
    source.write_bytes(b"changed research inputs")
    with pytest.raises(ValueError):
        preregister(tmp_path, source)


def test_minute_and_daily_prices_share_factor_base_and_raw_arrays_stay_raw():
    raw = bars()
    times = pd.DatetimeIndex(raw.bar_end.iloc[3::4])
    scale = np.r_[1.0, np.full(len(times) - 1, 2.0)]
    close = raw.close.iloc[3::4].to_numpy(copy=True)
    frozen = MarketSeries(
        "600000.SH",
        "样本",
        times[0],
        times,
        dict(scale=scale, close=close, amount=np.full(len(times), 1e8)),
        [],
    )
    # Minute availability starts later, after a factor change. An independent
    # first-minute anchor would incorrectly divide all its indicators by two.
    minute = raw.iloc[4:].copy()
    original = minute.copy(deep=True)
    prepared, issue = prepare_swing(minute, frozen)
    assert issue is None
    expected = (minute.close * 2).rolling(21).mean()
    expected.index = minute.bar_end
    expected = expected.reindex(times)
    np.testing.assert_allclose(prepared.data["trail_a"], expected, equal_nan=True)
    np.testing.assert_array_equal(prepared.data["close"], close)
    pd.testing.assert_frame_equal(minute, original)


def test_missing_real_factor_date_is_rejected_without_fill():
    raw = bars()
    times = pd.DatetimeIndex(raw.bar_end.iloc[3::4])[:-1]
    frozen = MarketSeries(
        "600000.SH",
        "样本",
        times[0],
        times,
        dict(scale=np.ones(len(times)), amount=np.full(len(times), 1e8)),
        [],
    )
    prepared, issue = prepare_swing(raw, frozen)
    assert prepared is None and "复权" in issue
