"""Causal completed-60F confirmation arrays for the staged execution model."""

from skills.quant.prepare import MarketSeries, swing_frame
from skills.trend.swing import indicators


def attach_confirmations(minute, stock):
    m, issue = swing_frame(minute, stock)
    if issue:
        raise ValueError(issue)
    f = indicators(m)
    eps = 1e-8 * f.atr
    f["scale_in_ready"] = (
        f.close.ge(f.ma8)
        & f.ma8.diff(2).gt(eps)
        & f.close.ge(f.ma13)
        & (f.ma13 - f.ma21).ge(-0.25 * f.atr)
    )
    f["tranche_trend_failed"] = (
        (f.close.lt(f.ma21 - eps) & f.ma13.diff(2).lt(-eps)).rolling(2).sum().eq(2)
    )
    count = f.groupby(f.bar_end.dt.normalize()).bar_end.transform("size")
    closing = f[
        f.bar_end.dt.hour.eq(15) & f.bar_end.dt.minute.eq(0) & count.eq(4)
    ].set_index("bar_end")
    aligned = closing.reindex(stock.times)
    data = dict(stock.data)
    for key in ["scale_in_ready", "tranche_trend_failed"]:
        data[key] = aligned[key].fillna(False).to_numpy(dtype=bool)
    return MarketSeries(
        stock.code,
        stock.name,
        stock.observed_at,
        stock.times.copy(),
        data,
        stock.signals,
    )
