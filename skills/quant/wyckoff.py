"""Adapt native price/volume evidence to after-work staged execution."""

from dataclasses import replace

import numpy as np
import pandas as pd

from skills.quant.prepare import swing_frame
from skills.trend.wyckoff import WyckoffConfig, events, features


def prepare_wyckoff(minute, stock, cfg=WyckoffConfig(), keep_entries=False, after=None):
    if "adjustment" in minute and not minute.adjustment.eq("none").all():
        raise ValueError("威科夫研究只接收未复权原生分钟行情")
    frame, issue = swing_frame(minute, stock)
    if issue:
        raise ValueError(issue)
    # Constant-factor-share volume keeps a mechanical split from becoming SOS.
    # Dividend factors remain an approximation, as in the execution ledger.
    frame["volume"] = frame.volume / frame.scale
    f = events(features(frame), cfg)
    count = f.groupby(f.bar_end.dt.normalize()).bar_end.transform("size")
    closing = f[
        (f.bar_end.dt.hour.eq(15)) & f.bar_end.dt.minute.eq(0) & count.eq(4)
    ].set_index("bar_end")
    daily = closing.reindex(stock.times)
    data = dict(stock.data)
    for key in (
        "supply_warning",
        "supply_defense",
        "structure_failed",
        "demand_ready",
        "attack",
    ):
        data["wyckoff_" + key] = daily[key].eq(True).to_numpy(dtype=bool)
    data["confirmed_higher_low"] = daily.support.to_numpy(dtype=float, copy=True)
    data["higher_low_origin"] = pd.DatetimeIndex(daily.support_origin).asi8.copy()
    data["higher_low_known"] = pd.DatetimeIndex(daily.support_known).asi8.copy()
    data["trail_a"] = data["confirmed_higher_low"].copy()
    data["trail_b"] = data["trail_a"].copy()
    data["atr"] = daily.atr.to_numpy(dtype=float, copy=True)
    data["scale_in_ready"] = data["wyckoff_attack"].copy()
    data["tranche_trend_failed"] = data["wyckoff_structure_failed"].copy()
    signals = []
    published = f if after is None else f[f.bar_end.gt(after)]
    by_day = {
        day: part for day, part in published.groupby(published.bar_end.dt.normalize())
    }
    at = {t: i for i, t in enumerate(stock.times)}
    for date, day in by_day.items():
        end = date + pd.Timedelta(hours=15)
        i = at.get(end)
        if i is None or len(day) != 4 or day.bar_end.iloc[-1] != end:
            continue
        attack = day[day.attack]
        held = False
        if not attack.empty and data["wyckoff_demand_ready"][i]:
            last = attack.iloc[-1]
            held = bool(
                day.close.iloc[-1] >= last.close - cfg.demand_hold_atr * last.atr
                and day[day.bar_end >= last.bar_end].low.min()
                > last.support - cfg.stop_buffer_atr * last.atr
            )
        data["scale_in_ready"][i] = data["wyckoff_attack"][i] = held
    if not keep_entries:
        eligible = published[
            published.event.ne("")
            & published.attack
            & published.score.ge(cfg.minimum_score)
        ]
        for date, candidates in eligible.groupby(eligible.bar_end.dt.normalize()):
            end = date + pd.Timedelta(hours=15)
            i = at.get(end)
            day = by_day[date]
            if (
                i is None
                or len(day) != 4
                or day.bar_end.iloc[-1] != end
                or stock.data["amount"][i] < cfg.minimum_daily_amount
                or day.supply_warning.iloc[-1]
                or day.structure_failed.iloc[-1]
            ):
                continue
            row = candidates.iloc[-1]
            if day.close.iloc[-1] < row.close - cfg.demand_hold_atr * row.atr:
                continue
            stop = float(row.event_floor - cfg.stop_buffer_atr * row.atr)
            close = float(day.close.iloc[-1])
            risk = (close - stop) / close * 100
            if (
                not 0 < risk <= cfg.maximum_risk_pct
                or day[day.bar_end >= row.bar_end].low.min() <= stop
            ):
                continue
            evidence = dict(
                method="wyckoff_price_volume_proxy",
                event=row.event,
                native_event_time=row.bar_end.isoformat(),
                support_origin=row.event_origin.isoformat(),
                support_known=row.bar_end.isoformat(),
                effort_ratio=float(row.effort),
                supply_contraction=float(row.contraction),
                close_location=float(row.close_location),
                selection=dict(
                    quality_score=float(row.score),
                    priority_score=float(row.score),
                    quality_scale="wyckoff-native-0-100",
                    minimum_quality_score=cfg.minimum_score,
                ),
            )
            signals.append(
                dict(
                    i=i,
                    family="base_rebound"
                    if row.event == "spring_test"
                    else "trend_pullback",
                    preferred=True,
                    leg=row.event_origin.isoformat(),
                    count=1,
                    stop=stop,
                    score=float(row.score),
                    quality_score=float(row.score),
                    evidence=evidence,
                )
            )
    return replace(stock, data=data, signals=stock.signals if keep_entries else signals)
