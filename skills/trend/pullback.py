"""Track controlled daily pullbacks and preserve first/subsequent round labels."""

import pandas as pd


def controlled_pullbacks(frame):
    f = frame
    prior_low = f.low.shift().rolling(10).min()
    up = (f.ma13 > f.ma21) & (f.ma21.diff(3) > 1e-8 * f.atr) & (f.close > f.ma13)
    records = []
    leg = None
    up_count = 0
    for i, r in enumerate(f.itertuples()):
        up_count = up_count + 1 if up.iloc[i] else 0
        if leg and r.close < r.ma21 - r.atr:
            leg = None
        if leg is None and up_count >= 5:
            leg = dict(
                start=i,
                floor=prior_low.iloc[i],
                peak=r.high,
                pull=None,
                finished=False,
                rounds=0,
                resumed=-1,
            )
        active = False
        if leg:
            old_peak = leg["peak"]
            if leg["finished"] and r.close > old_peak and i - leg["resumed"] >= 3:
                leg.update(pull=None, finished=False)
            if (
                not leg["finished"]
                and leg["pull"] is None
                and i > leg["start"]
                and old_peak - leg["floor"] >= 2 * r.atr
                and r.close < f.close.iloc[i - 1]
                and old_peak - r.low >= 0.5 * r.atr
            ):
                leg["pull"] = i
                leg["rounds"] += 1
            if leg["pull"] is not None and not leg["finished"]:
                depth = old_peak - r.low
                active = (
                    i - leg["pull"] <= 10
                    and depth <= 3 * r.atr
                    and r.low >= r.ma21 - r.atr
                )
                if not active:
                    leg["finished"] = True
                elif i > leg["pull"] and r.close > f.high.iloc[i - 1]:
                    # Offer the first resumption bar, even when its other gates
                    # fail. A second pullback must not become another "first".
                    leg["finished"] = True
                    leg["resumed"] = i
            leg["peak"] = max(old_peak, r.high)
        records.append(
            dict(
                active=bool(active),
                rounds=leg["rounds"] if leg else 0,
                first=bool(active and leg["rounds"] == 1),
            )
        )
    return pd.DataFrame(records, index=f.index)


def first_pullback(frame):
    return controlled_pullbacks(frame)["first"]
