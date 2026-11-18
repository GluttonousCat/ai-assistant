"""Apply 60F moving-average context to LPS entries, leaving springs independent."""

from dataclasses import replace

import numpy as np
import pandas as pd

from skills.quant.prepare import swing_frame


def combine_families(ranked, volume):
    """Join original trend campaigns with price/volume-only reversal campaigns."""
    by_code = {stock.code: stock for stock in volume}
    if (
        len(by_code) != len(volume)
        or len({stock.code for stock in ranked}) != len(ranked)
        or set(by_code) != {stock.code for stock in ranked}
    ):
        raise ValueError("混合入场股票池不一致或代码重复")
    result = []
    for stock in ranked:
        other = by_code[stock.code]
        if not stock.times.equals(other.times) or any(
            not np.array_equal(stock.data[key], other.data[key], equal_nan=True)
            for key in ("open", "high", "low", "close", "scale")
        ):
            raise ValueError(f"{stock.code} 混合入场时间或报价基准不一致")
        signals = [
            signal for signal in stock.signals if signal["family"] == "trend_pullback"
        ]
        signals.extend(
            signal for signal in other.signals if signal["family"] == "base_rebound"
        )
        if any(
            not np.isfinite(s["quality_score"]) or s["quality_score"] < 72
            for s in signals
        ):
            raise ValueError(f"{stock.code} 混合候选未通过原始质量筛选")
        data = {
            **stock.data,
            **{
                key: other.data[key]
                for key in (
                    "wyckoff_attack",
                    "wyckoff_demand_ready",
                    "wyckoff_supply_warning",
                    "wyckoff_supply_defense",
                    "wyckoff_structure_failed",
                    "confirmed_higher_low",
                    "higher_low_origin",
                    "higher_low_known",
                )
            },
            "wyckoff_atr": other.data["atr"],
        }
        result.append(replace(stock, data=data, signals=signals))
    return result


def filter_pullbacks(minute, stock):
    """Evaluate M8/M13/M21 at the exact completed LPS confirmation bar."""
    if not any(signal["family"] == "trend_pullback" for signal in stock.signals):
        return stock
    frame, issue = swing_frame(minute, stock)
    if issue:
        raise ValueError(issue)
    close = frame.set_index("bar_end").close
    fast, mid, slow = (close.rolling(period).mean() for period in (8, 13, 21))
    tolerance = 1e-10 * close
    context = pd.DataFrame({"m8": fast, "m13": mid, "m21": slow})
    context["confirmed"] = (
        (mid - slow).gt(tolerance)
        & slow.diff(4).gt(tolerance)
        & fast.diff(2).gt(tolerance)
        & close.ge(fast - tolerance)
    )
    signals = []
    for signal in stock.signals:
        if signal["family"] == "base_rebound":
            signals.append(signal)
            continue
        evidence = signal["evidence"]
        trigger = pd.Timestamp(evidence["native_event_time"])
        if (
            evidence["event"] != "lps"
            or trigger > stock.times[signal["i"]]
            or trigger not in context.index
        ):
            raise ValueError(f"{stock.code} 回调缺少可核验的60F确认时点")
        row = context.loc[trigger]
        if row.confirmed:
            signals.append(
                {
                    **signal,
                    "evidence": {
                        **evidence,
                        "trend_context": {
                            "at": trigger.isoformat(),
                            **{key: float(row[key]) for key in ("m8", "m13", "m21")},
                        },
                    },
                }
            )
    return replace(stock, signals=signals)
