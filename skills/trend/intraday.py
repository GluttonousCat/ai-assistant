"""Closed 60F observations; daily-entry confirmation is never manufactured."""
import math
import json
from datetime import timedelta

import pandas as pd

from skills.trend.mtf import MtfConfig, minute_context
from skills.trend.parent_context import annotate_summary
from skills.trend.chart_history import five_year_start
from tools.market.trend_data import adjust_prices, intraday_report, intraday_schedule, scan_inputs, scan_daily_inputs


def rank_candidates(rows):
    return sorted(rows, key=lambda row: (-row["score"], row["ts_code"]))


def dashboard():
    report = intraday_report()
    if not report:
        return None
    return {**report, "data": rank_candidates(report["data"]), "schedule": intraday_schedule()}


def chart(code, run_id=None, timeframe="60F"):
    if timeframe not in ("1D", "60F"):
        raise ValueError("仅支持日K与60F")
    # Read the requested archive even while the next session is being published.
    report = intraday_report(run_id)
    if not report or not any(r["ts_code"] == code for r in report["data"]):
        raise LookupError("该股票不在已发布盘中观察名单中")
    cutoff, daily_cutoff = pd.Timestamp(report["as_of"]), pd.Timestamp(report["daily_as_of"])
    if daily_cutoff >= cutoff.normalize():
        raise ValueError("盘中日K背景必须早于当天")
    start = report.get("start", "2026-01-01")
    raw = scan_inputs([code], start, str(cutoff))
    raw = raw[pd.to_datetime(raw.bar_end) <= cutoff].copy() if not raw.empty else raw
    minute, issue = adjust_prices(raw)
    if issue or pd.Timestamp(minute.bar_end.max()) != cutoff:
        return {"data": [], "issue": issue or "该时点60F不完整"}
    frame = minute
    if timeframe == "1D":
        begin = five_year_start(str(daily_cutoff))
        raw_daily = scan_daily_inputs([code], str(begin - timedelta(days=70)), str(daily_cutoff))
        raw_daily = raw_daily[pd.to_datetime(raw_daily.bar_end) <= daily_cutoff].copy() if not raw_daily.empty else raw_daily
        frame, issue = adjust_prices(raw_daily)
        if issue or pd.Timestamp(frame.bar_end.max()) != daily_cutoff:
            return {"data": [], "issue": issue or "此前完整日K背景不足"}
        # Native daily and minute bars share today's genuine ex-rights anchor.
        scale = float(frame.adj_factor.iloc[-1]) / float(minute.adj_factor.iloc[-1])
        frame[["open", "high", "low", "close"]] *= scale
    for window in (8, 13, 21):
        frame[f"ma{window}"] = frame.close.rolling(window, min_periods=window).mean()
    if timeframe == "1D":
        frame = frame[pd.to_datetime(frame.bar_end).dt.date >= begin]
    else:
        frame = frame.tail(1000)
    fields = ["bar_end", "open", "high", "low", "close", "volume", "amount", "ma8", "ma13", "ma21"]
    bars = json.loads(frame[fields].to_json(orient="records", date_format="iso"))
    origin = next(r for r in report["data"] if r["ts_code"] == code)["trend60"]["origin"]
    for bar in bars:
        bar["eligible"] = False
        bar["tracking_signal"] = pd.Timestamp(bar["bar_end"]) == pd.Timestamp(origin) if timeframe == "60F" else str(bar["bar_end"])[:10] == str(origin)[:10]
    return {"run_id": report["run_id"], "code": code, "as_of": str(cutoff),
            "daily_as_of": str(daily_cutoff), "timeframe": timeframe, "data": bars,
            "config": {"selection_mode": "tracking"}, "history_years": 5 if timeframe == "1D" else None}


def observe(minute, daily, cutoff, daily_cutoff, config):
    minute, mi = adjust_prices(minute)
    daily, di = adjust_prices(daily)
    if mi or di:
        return {"status": "data_missing", "issue": mi or di}
    # Use today's common factor anchor when an ex-rights event separates the
    # last complete daily bar from today's minute prices.
    scale = float(daily.adj_factor.iloc[-1]) / float(minute.adj_factor.iloc[-1])
    daily[["open", "high", "low", "close"]] *= scale
    if pd.Timestamp(minute.bar_end.max()) != pd.Timestamp(cutoff) or pd.Timestamp(daily.bar_end.max()) != pd.Timestamp(daily_cutoff):
        return {"status": "data_missing", "issue": "60F或完整日K截止不符（含停牌/复牌）"}
    if pd.Timestamp(daily.bar_end.max()) >= pd.Timestamp(cutoff).normalize():
        raise ValueError("盘中观察只能使用此前已完成的日K背景")
    cfg = MtfConfig(**config)
    evaluated = minute_context(minute, cfg)
    last = evaluated.iloc[-1]
    if not cfg.fresh_reversal or not bool(last.get("reversal_fresh", False)):
        return {"status": "filtered", "issue": "没有现行规则的低位早期60F结构"}
    close, atr, floor = float(last.close), float(last.atr), float(last.reversal_floor)
    if not all(math.isfinite(v) and v > 0 for v in (close, atr, floor)):
        return {"status": "data_missing", "issue": "结构或波动预热不足"}
    stop = min(floor - .15 * atr, close - cfg.stop_atr * atr)
    risk = (close - stop) / close * 100
    liquidity = float(daily.amount.tail(20).mean()) if len(daily) >= 20 else 0.
    trend_ok = bool(last.trend_ready)
    risk_ok = stop > 0 and 0 < risk <= cfg.max_risk_pct
    extension = (close - float(last[f"ma{cfg.mid}"])) / atr
    extension_ok = extension <= cfg.max_extension_atr
    if not trend_ok or not risk_ok or not extension_ok or liquidity < cfg.min_daily_amount:
        return {"status": "filtered", "issue": "60F修复/结构风险/偏离/昨日流动性尚未通过"}
    origin = pd.Timestamp(last.reversal_start)
    today = evaluated[pd.to_datetime(evaluated.bar_end).dt.date == pd.Timestamp(cutoff).date()]
    summary = {"status": "candidate", "eligible": False, "setup_watch": True,
        "state": "盘中观察 · 待盘后复核", "pattern": "base_rebound",
        "score": round(40 + 20 + 10 + 10 * max(0., 1 - risk / cfg.max_risk_pct), 1),
        "score_scope": "盘中观察最高80分，未计未完成日K的20分",
        "signal_date": str(origin)[:10], "new_today": origin.date() == pd.Timestamp(cutoff).date(),
        "bar_end": str(last.bar_end), "stop_price": round(stop, 6), "risk_pct": round(risk, 4),
        "metrics": {"close": close, "atr": atr, "ma8": float(last.ma8), "ma13": float(last.ma13),
                    "ma21": float(last.ma21), "volume_ratio": float(last.volume_ratio) if pd.notna(last.volume_ratio) else None,
                    "daily_amount": liquidity},
        "trend60": {"ready": trend_ok, "fresh": True, "origin": origin.isoformat(),
                    "age_bars": int(last.reversal_age), "bullish_count": int(last.reversal_bulls),
                    "gain_atr": float(last.reversal_gain_atr), "state": str(last.reversal_state)},
        "today_bars": [{"bar_end": str(r.bar_end), "open": float(r.open), "close": float(r.close),
                        "high": float(r.high), "low": float(r.low), "volume": float(r.volume)} for r in today.itertuples()],
        "reason": f"60F早期结构第{int(last.reversal_age)}根，推进{int(last.reversal_bulls)}阳；M8转升且收于M8上方，结构风险{risk:.2f}%，仅作盘中观察",
        "checks": [{"label": "已完成60F修复", "passed": True}, {"label": "结构止损与风险距离", "passed": True},
                   {"label": "昨日20日平均成交额", "passed": True}, {"label": "完整日K入场确认", "passed": False}]}
    return annotate_summary(summary, daily, daily_cutoff)
