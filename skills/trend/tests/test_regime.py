import pandas as pd

from skills.trend.regime import breadth, closing_context, allowed_signals
from skills.trend.tests.test_swing import bars
from skills.quant.prepare import MarketSeries


def test_future_observations_never_change_past_breadth():
    times = pd.date_range("2026-01-05", periods=3)
    a = pd.DataFrame(dict(above=[0.0, 1.0, 1.0]), index=times)
    future = pd.DataFrame(dict(above=[0.0]), index=times[-1:])
    old = breadth({"a": a})
    new = breadth({"a": a, "future_ipo": future})
    pd.testing.assert_frame_equal(old.iloc[:2], new.iloc[:2])


def test_closing_context_is_prefix_causal_and_rejects_missing_slots():
    raw = bars()
    full = closing_context(raw)
    prefix = closing_context(raw.iloc[:73])
    pd.testing.assert_frame_equal(prefix, full.loc[prefix.index])
    missing = closing_context(raw.drop(raw.index[-2]))
    assert raw.bar_end.iloc[-1] not in missing.index


def test_flat_prices_never_become_strength_after_factor_rebasing():
    for scale in [1.0, 1 / 7, 1e4]:
        raw = bars()
        for field, value in [
            ("open", 97.3),
            ("close", 97.3),
            ("high", 97.4),
            ("low", 97.2),
        ]:
            raw[field] = value * scale
        context = closing_context(raw)
        assert not context.reversal_safe.any()
        assert not context.pullback_safe.any()
        assert context.above.fillna(0).sum() == 0


def test_regime_uses_signal_close_not_next_days_repair():
    times = pd.date_range("2026-01-05 15:00", periods=3)
    context = pd.DataFrame(
        dict(pullback_safe=[False, True, True], reversal_safe=[False, True, True]),
        index=times,
    )
    market = pd.DataFrame(
        dict(available=[200, 200, 200], fraction=[0.8, 0.8, 0.8]), index=times
    )
    signals = [dict(i=0, family="trend_pullback"), dict(i=1, family="trend_pullback")]
    stock = MarketSeries("600000.SH", "样本", times[0], times, {}, signals)
    result = allowed_signals(stock, context, market, 0.6)
    assert result == [signals[1]] and len(stock.signals) == 2
