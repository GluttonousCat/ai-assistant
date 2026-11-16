from copy import deepcopy
import json

import numpy as np
import pandas as pd

from skills.trend.parent_context import daily_context, annotate_chart, annotate_summary


def wave():
    close = np.r_[
        np.full(35, 100.0), np.linspace(100, 140, 35), np.linspace(139, 121, 22)
    ]
    return pd.DataFrame(
        dict(
            bar_end=pd.bdate_range("2026-01-05", periods=len(close))
            + pd.Timedelta(hours=15),
            open=close - 0.2,
            high=close + 0.5,
            low=close - 0.5,
            close=close,
            volume=np.full(len(close), 1000),
            amount=np.full(len(close), 1e8),
        )
    )


def test_long_pullback_keeps_daily_parent_even_after_short_daily_averages_turn_down():
    context = daily_context(wave())
    last = context.iloc[-1]
    assert last.parent_trend == "pullback" and last.parent_pullback_bars >= 20
    assert last.parent_ma13 < last.parent_ma21


def test_prefix_and_rebase_do_not_repaint_parent_context():
    raw = wave()
    full = daily_context(raw)
    prefix = daily_context(raw.iloc[:84])
    pd.testing.assert_frame_equal(prefix, full.iloc[:84])
    changed = raw.copy()
    changed[["open", "high", "low", "close"]] *= 7
    rebased = daily_context(changed)
    assert list(full.parent_trend) == list(rebased.parent_trend)
    assert list(full.parent_pullback_bars) == list(rebased.parent_pullback_bars)


def test_intraday_context_cannot_see_that_days_unfinished_daily_close():
    raw = wave()
    rows = pd.DataFrame(
        dict(
            bar_end=[
                raw.bar_end.iloc[-1] - pd.Timedelta(hours=4, minutes=30),
                raw.bar_end.iloc[-1],
            ],
            pattern=["base_rebound"] * 2,
            reversal_ignition=[True] * 2,
            eligible=[False] * 2,
        )
    )
    before = rows.copy(deep=True)
    annotated = annotate_chart(rows, raw)
    assert annotated.parent_context_end.iloc[0] == raw.bar_end.iloc[-2]
    assert annotated.parent_context_end.iloc[1] == raw.bar_end.iloc[-1]
    assert annotated.context_pattern.eq("trend_pullback").all()
    assert annotated.local_trigger_label.eq("局部止跌反转").all()
    pd.testing.assert_frame_equal(annotated[before.columns], before)


def test_broken_parent_anchor_is_not_held_forever_as_a_pullback():
    raw = wave()
    tail = raw.iloc[-2:].copy()
    tail.bar_end = pd.bdate_range(
        raw.bar_end.iloc[-1].normalize() + pd.offsets.BDay(), periods=2
    ) + pd.Timedelta(hours=15)
    tail[["open", "high", "low", "close"]] = [80, 81, 79, 80]
    last = daily_context(pd.concat([raw, tail], ignore_index=True)).iloc[-1]
    assert last.parent_trend not in ("uptrend", "pullback")


def test_snapshot_annotation_preserves_original_signal_and_is_json_serializable():
    raw = wave()
    source = dict(
        pattern="base_rebound",
        pattern_label="阴跌反转",
        eligible=True,
        score=80,
        state="日K入场候选",
        stop_price=119,
        checks=[dict(passed=True)],
    )
    before = deepcopy(source)
    annotated = annotate_summary(source, raw, raw.bar_end.iloc[-1])
    assert source == before and all(annotated[k] == v for k, v in before.items())
    assert annotated["context_pattern"] == "trend_pullback"
    json.dumps(annotated, allow_nan=False)
    cutoff = raw.bar_end.iloc[83]
    assert annotate_summary(source, raw, cutoff) == annotate_summary(
        source, raw.iloc[:84], cutoff
    )


def test_missing_parent_data_does_not_invent_an_uptrend_or_change_original_family():
    source = dict(pattern="base_rebound", eligible=False)
    result = annotate_summary(source, pd.DataFrame(), "2026-11-12")
    assert result["parent_trend"] == "unknown" and result["pattern"] == "base_rebound"
    assert "context_pattern" not in result
    stale = annotate_summary(
        {**source, "context_pattern": "trend_pullback", "parent_trend": "uptrend"},
        pd.DataFrame(),
        "2026-11-12",
    )
    assert "context_pattern" not in stale and stale["parent_trend"] == "unknown"
