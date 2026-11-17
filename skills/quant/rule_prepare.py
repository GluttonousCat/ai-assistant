"""Compile declarative rules into daily execution inputs with causal evidence."""

import numpy as np
import pandas as pd

from skills.quant.prepare import MarketSeries, swing_frame
from skills.quant.rule_indicators import Evaluator

VERSION = "composable-rules-next-open-v1.1"
SLOTS = {(10, 30), (11, 30), (14, 0), (15, 0)}


def prepare_rules(minute, frozen, rules):
    daily = pd.DataFrame(
        {
            "bar_end": frozen.times,
            **{
                k: frozen.data["adjusted_" + k]
                for k in ("open", "high", "low", "close")
            },
            "volume": frozen.data["volume"],
            "amount": frozen.data["amount"],
        }
    )
    daily["raw_close"] = frozen.data["close"].copy()
    if rules.needs_minutes():
        raw_close = pd.to_numeric(
            minute.set_index("bar_end").close, errors="coerce"
        ).copy()
        minute, issue = swing_frame(minute, frozen)
        if issue:
            return None, issue
        minute["raw_close"] = minute.bar_end.map(raw_close)
        # Drop incomplete/foreign-slot days before evaluating indicators.
        days = minute.bar_end.dt.normalize()
        valid = minute.groupby(days).bar_end.apply(
            lambda stamps: len(stamps) == 4
            and {(t.hour, t.minute) for t in stamps} == SLOTS
        )
        complete = days.map(valid).fillna(False)
        minute = minute.loc[complete].copy()
        clock = pd.DatetimeIndex(minute.bar_end)
    else:
        minute = pd.DataFrame()
        clock = frozen.times.copy()
    if clock.empty:
        return None, "没有完整的规则评估K线"
    evaluator = Evaluator(minute, daily, clock)
    entry = evaluator.evaluate(rules.entry)
    if rules.sampling == "at_close":
        entry &= (clock.hour == 15) & (clock.minute == 0)
    if rules.trigger == "edge":
        sampled = (
            entry.loc[(clock.hour == 15) & (clock.minute == 0)]
            if rules.sampling == "at_close"
            else entry
        )
        events = sampled & ~sampled.shift(fill_value=False)
        entry = events.reindex(clock, fill_value=False)
    accepted, previous = [], -rules.cooldown_bars - 1
    for i, hit in enumerate(entry):
        if hit and i - previous >= rules.cooldown_bars:
            accepted.append(i)
            previous = i
    signals = []
    amount = (
        pd.Series(frozen.data["amount"], index=frozen.times)
        .rolling(20, min_periods=20)
        .mean()
    )
    for i in accepted:
        stamp = clock[i]
        end = stamp.normalize() + pd.Timedelta(hours=15)
        daily_i = frozen.times.get_indexer([end])[0]
        if daily_i < 0 or (
            rules.min_daily_amount
            and not amount.get(end, np.nan) >= rules.min_daily_amount
        ):
            continue
        signals.append(
            dict(
                i=int(daily_i),
                family="custom_rule",
                preferred=True,
                leg=stamp.isoformat(),
                count=1,
                stop=float(
                    frozen.data["adjusted_close"][daily_i]
                    * (1 - rules.stop_loss_pct / 100)
                ),
                score=100.0,
                evidence={
                    "source": "rules-v1",
                    "triggered_at": stamp.isoformat(),
                    "rule": rules.entry.model_dump(),
                },
            )
        )
    # Multiple intraday events become one end-of-day order per stock.
    signals = list({s["i"]: s for s in signals}.values())
    exits = evaluator.evaluate(rules.exit)
    if rules.sampling == "at_close":
        exits &= (clock.hour == 15) & (clock.minute == 0)
    by_day = exits.groupby(clock.normalize()).any()
    data = dict(frozen.data)
    data["rule_exit"] = by_day.reindex(
        frozen.times.normalize(), fill_value=False
    ).to_numpy(dtype=bool)
    for key in ("trail_a", "trail_b", "atr"):
        data[key] = np.full(len(frozen.times), np.nan)
    return MarketSeries(
        frozen.code, frozen.name, frozen.observed_at, frozen.times.copy(), data, signals
    ), None
