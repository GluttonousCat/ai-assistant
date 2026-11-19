import numpy as np
import pytest
from skills.quant.prepare import prepare_stock
from skills.quant.metrics import summarize
from tests.skills.trend.test_dual_setups import make_setup


@pytest.mark.parametrize("period", ["60F", "1D"])
def test_future_adjustment_does_not_rebase_past_features(period):
    f = make_setup("pit", period)
    member = {
        "ts_code": "000001.SZ",
        "name": "样本",
        "observed_at": "2026-11-12T17:08:00",
    }
    f["up_limit"] = f.close * 1.1
    f["down_limit"] = f.close * 0.9
    first, _ = prepare_stock(f.iloc[:-3], member, period)
    f.loc[f.index[-3:], "adj_factor"] = 2
    f.loc[f.index[-3:], ["open", "high", "low", "close"]] /= 2
    full, issue = prepare_stock(f, member, period)
    assert issue is None
    np.testing.assert_allclose(
        first.data["adjusted_close"], full.data["adjusted_close"][:-3]
    )
    assert first.signals == [s for s in full.signals if s["i"] < len(f) - 3]


def test_missing_real_factor_is_not_filled():
    f = make_setup("pit")
    f["up_limit"] = 100
    f["down_limit"] = 1
    f.loc[f.index[20], "adj_factor"] = np.nan
    stock, issue = prepare_stock(f, {"ts_code": "000001.SZ"}, "60F")
    assert stock is None and "不完整" in issue


def test_factor_adjustment_never_mutates_raw_execution_prices():
    f = make_setup("pit")
    f["up_limit"] = 100
    f["down_limit"] = 1
    cut = len(f) // 2
    f.loc[cut:, "adj_factor"] = 2
    f.loc[cut:, ["open", "high", "low", "close"]] /= 2
    original = f.copy(deep=True)
    s, issue = prepare_stock(
        f, {"ts_code": "000001.SZ", "name": "样本", "observed_at": "2026-01-01"}, "60F"
    )
    assert issue is None
    for key in ["open", "high", "low", "close"]:
        np.testing.assert_array_equal(s.data[key], original[key].to_numpy())
        np.testing.assert_allclose(
            s.data["adjusted_" + key], original[key] * s.data["scale"]
        )
        assert not np.shares_memory(s.data[key], s.data["adjusted_" + key])
    assert f.equals(original)


def test_nav_drawdown_and_profit_factor_use_different_units():
    equity = [{"equity": 90.0, "exposure": 0.5}, {"equity": 110.0, "exposure": 0.8}]
    trades = [
        {"pnl": 20.0, "return_pct": 20.0},
        {"pnl": -10.0, "return_pct": -10.0},
        {"pnl": 0.0, "return_pct": 0.0},
    ]
    r = summarize(equity, trades, 100.0, 0)
    assert r["total_return_pct"] == pytest.approx(10)
    assert r["max_drawdown_pct"] == pytest.approx(10)
    assert r["profit_factor"] == 2
    assert r["win_rate_pct"] == pytest.approx(100 / 3)
    assert r["annualized_return_pct"] is None
