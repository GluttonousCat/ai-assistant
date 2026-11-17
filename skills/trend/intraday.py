"""Closed 60F observations; daily-entry confirmation is never manufactured."""
import math
import base64

import pandas as pd

from skills.trend.mtf import MtfConfig, minute_context
from skills.trend.parent_context import annotate_summary
from tools.market.trend_data import adjust_prices, intraday_report, intraday_chart


def dashboard():
    return intraday_report()


def chart(code, run_id=None):
    report = intraday_report()
    if report and run_id and report["run_id"] != run_id:
        raise LookupError("盘中观察快照已更新，请刷新名单")
    if not report or not any(r["ts_code"] == code for r in report["data"]):
        raise LookupError("该股票不在已发布盘中观察名单中")
    png = intraday_chart(report["run_id"], code)
    if not png:
        raise LookupError("该股票盘中图表尚未生成")
    return {"run_id": report["run_id"], "code": code, "as_of": report["as_of"],
            "image": "data:image/png;base64," + base64.b64encode(png).decode()}


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
