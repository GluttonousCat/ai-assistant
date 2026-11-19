"""Follow frozen selections with subsequent quotes; never simulate a trade."""
from datetime import datetime, timedelta
import math
from zoneinfo import ZoneInfo

from skills.trend.digest import select_stocks
from skills.trend.chart_history import stock_chart as stock_detail
from tools.market.trend_data import load_dashboard, scan_daily_inputs, tracking_calendar, tracking_days

HORIZONS = (1, 3, 5, 10)


def completed_end(now=None):
    now = now or datetime.now(ZoneInfo("Asia/Shanghai"))
    if now.tzinfo:
        now = now.astimezone(ZoneInfo("Asia/Shanghai"))
    day = now.date() if now.hour >= 15 else (now - timedelta(days=1)).date()
    return f"{day} 15:00:00"


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (ValueError, TypeError):
        return None


def performance(bars, signal_date, calendar):
    """Ratios use each bar's true factor. Suspension never advances a horizon."""
    by_day = {str(r["bar_end"])[:10]: r for r in bars}
    base = by_day.get(signal_date, {})
    close, factor = _number(base.get("close")), _number(base.get("adj_factor"))
    denominator = close * factor if close and factor else None
    if denominator is not None and not math.isfinite(denominator):
        denominator = None
    after = [day for day in calendar if day > signal_date]
    result = {"baseline_date": signal_date, "baseline_close": close, "elapsed_sessions": len(after),
              "latest_date": None, "latest_return_pct": None, "max_up_pct": None,
              "max_down_pct": None, "path": [], "horizons": {}, "issue": None}
    if not denominator:
        result["issue"] = "入选日原生收盘价或真实复权因子缺失"
    elif signal_date not in calendar:
        result["issue"] = "入选日交易日历缺失，不能确认后续交易日序号"
        result["horizons"] = {str(n): {"date": None, "return_pct": None, "status": "missing"} for n in HORIZONS}
        return result

    def change(bar, field):
        price, adjustment = _number(bar.get(field)), _number(bar.get("adj_factor"))
        value = (price * adjustment / denominator - 1) * 100 if denominator and price and adjustment else None
        return round(value, 4) if value is not None and math.isfinite(value) else None

    for horizon in HORIZONS:
        target = after[horizon - 1] if len(after) >= horizon else None
        value = change(by_day.get(target, {}), "close") if target else None
        result["horizons"][str(horizon)] = {
            "date": target, "return_pct": value,
            "status": "ready" if value is not None else "missing" if target else "pending",
        }
    if not denominator:
        return result
    result["path"] = [{"date": signal_date, "return_pct": 0.}]
    highs, lows = [], []
    gaps = 0
    for day in after:
        bar = by_day.get(day, {})
        value = change(bar, "close")
        result["path"].append({"date": day, "return_pct": value})
        if value is None:
            gaps += 1
            continue
        result["latest_date"], result["latest_return_pct"] = day, value
        high, low = change(bar, "high"), change(bar, "low")
        if high is not None:
            highs.append(high)
        if low is not None:
            lows.append(low)
    result["max_up_pct"] = max(highs) if highs else None
    result["max_down_pct"] = min(lows) if lows else None
    if gaps:
        result["issue"] = f"后续{gaps}个交易日无有效行情或因子（含停牌），区间高低仅统计有效记录"
    return result


def follow_up(signal_date=None, now=None):
    end = completed_end(now)
    days = tracking_days(end)
    if not days:
        return {"dates": [], "data": [], "signal_date": None, "as_of": end}
    available = [{**d, "date": str(d["as_of"])[:10]} for d in days]
    if signal_date:
        chosen = next((d for d in available if d["date"] == str(signal_date)), None)
        if not chosen:
            raise LookupError("该日期没有已发布的历史筛选快照")
    else:
        # Prefer a cohort with at least one completed follow-up session.
        chosen = next((d for d in available if d["date"] < end[:10]), available[0])
    minimum = 60
    snapshot = load_dashboard(chosen["run_id"], min_numeric_score=minimum)
    if not snapshot:
        raise LookupError("历史快照不可用")
    rows = select_stocks(snapshot, minimum=minimum, observations=True)
    calendar = tracking_calendar(chosen["date"], end[:10])
    frame = scan_daily_inputs([r["ts_code"] for r in rows], chosen["date"], end) if rows else None
    grouped = {code: part.to_dict(orient="records") for code, part in frame.groupby("ts_code")} if frame is not None and not frame.empty else {}
    results = [{**r, "follow_up": performance(grouped.get(r["ts_code"], []), chosen["date"], calendar)} for r in rows]
    ready = [r["follow_up"]["latest_return_pct"] for r in results if r["follow_up"]["latest_return_pct"] is not None]
    return {"dates": available, "signal_date": chosen["date"], "run_id": chosen["run_id"],
            "version": snapshot.get("version"), "published_at": snapshot.get("created_at"),
            "as_of": end, "data": results,
            "summary": {"count": len(results), "measured": len(ready),
                        "positive": sum(v > 0 for v in ready),
                        "mean_return_pct": round(sum(ready) / len(ready), 4) if ready else None}}


def follow_up_chart(code, run_id, timeframe="1D", now=None):
    snapshot = load_dashboard(run_id, code=code)
    if not snapshot or not any(r["ts_code"] == code for r in select_stocks(snapshot)):
        raise LookupError("该股票不在所选快照的60分候选名单中")
    end = completed_end(now)
    result = stock_detail(code, snapshot["metadata"]["start"], end,
                          snapshot["metadata"]["config"], timeframe)
    signal_date = str(snapshot["as_of"])[:10]
    signal_bars = [r for r in result["data"] if str(r["bar_end"])[:10] == signal_date]
    marker_end = str(signal_bars[-1]["bar_end"]) if signal_bars else None
    # Forward data are a quote review, not a fresh application of old signals.
    for bar in result["data"]:
        for key in ("eligible", "first_watch", "resume_watch", "swing_ignition", "reversal_ignition"):
            bar[key] = False
        bar["tracking_signal"] = str(bar["bar_end"]) == marker_end
    return {**result, "signal_date": signal_date, "run_id": run_id, "as_of": end,
            "signal_close": signal_bars[-1].get("close") if signal_bars else None}
