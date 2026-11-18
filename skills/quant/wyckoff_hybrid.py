"""Apply 60F moving-average context to LPS entries, leaving springs independent."""

from dataclasses import replace

import pandas as pd

from skills.quant.prepare import swing_frame


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
