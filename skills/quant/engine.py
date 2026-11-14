"""Long-only cash portfolio: prior-close decisions, next-bar orders, T+1 exits.

Intrabar brackets use precommitted levels; ambiguous stop/target touches take the
stop first. The model has no order book. Daily limits are actual provider data.
"""

from collections import Counter, defaultdict
from dataclasses import asdict
import numpy as np
import pandas as pd
from skills.quant.models import BacktestConfig
from skills.quant.metrics import summarize
from skills.quant.registry import STRATEGIES

EXECUTION_VERSION = "price-units-and-protected-stop-v3"


def opening_time(end, timeframe):
    end = pd.Timestamp(end)
    if timeframe == "1D" or (end.hour, end.minute) == (10, 30):
        return end.normalize() + pd.Timedelta(hours=9, minutes=30)
    if (end.hour, end.minute) == (14, 0):
        return end.normalize() + pd.Timedelta(hours=13)
    return end - pd.Timedelta(hours=1)


def simulate(
    series,
    timeline,
    strategy_id="trend_dual_v4",
    timeframe="60F",
    universe_mode="fixed_snapshot",
    cfg=BacktestConfig(),
):
    if (
        strategy_id not in STRATEGIES
        or universe_mode not in ("fixed_snapshot", "observed_only")
        or timeframe not in ("60F", "1D")
    ):
        raise ValueError("未知策略/成分模式/周期")
    timeline = pd.DatetimeIndex(timeline)
    if cfg.close_only and timeframe != "1D":
        raise ValueError("盘后执行模式使用原生日K撮合与交易日持有期")
    if cfg.exit_policy == "reversal" and timeframe != "1D":
        raise ValueError("反转分段离场使用原生日K，持有期按交易日计算")
    if (
        timeline.empty
        or not timeline.is_monotonic_increasing
        or timeline.has_duplicates
    ):
        raise ValueError("回测交易时间轴必须非空且严格递增")
    family = STRATEGIES[strategy_id]["family"]
    stocks = {s.code: s for s in series}
    for s in series:
        for field in ["open", "high", "low", "close"]:
            if not np.allclose(
                s.data[field] * s.data["scale"],
                s.data["adjusted_" + field],
                rtol=1e-10,
                atol=1e-8,
            ):
                raise ValueError(f"{s.code}原始价/复权价不一致，拒绝回测")
    orders = defaultdict(list)
    audit = Counter()
    for s in series:
        mapping = timeline.get_indexer(s.times)
        if (mapping < 0).any():
            raise ValueError(f"{s.code}行情不在交易日历时间轴内")
        s.row_at = np.full(len(timeline), -1, dtype=np.int32)
        s.row_at[mapping] = np.arange(len(mapping), dtype=np.int32)
        for sig in s.signals:
            if (
                family
                and sig["family"] != family
                or family is None
                and not sig["preferred"]
            ):
                continue
            i = sig["i"]
            if universe_mode == "observed_only" and s.times[i] < s.observed_at:
                audit["before_membership_observed"] += 1
                continue
            audit["signals"] += 1
            if i + 1 >= len(mapping) or mapping[i + 1] != mapping[i] + 1:
                audit["no_next_bar"] += 1
                continue
            orders[int(mapping[i + 1])].append((s.code, sig))
    cash = cfg.initial_cash
    positions = {}
    consumed = set()
    trades = []
    equity = []
    fees = 0.0

    def can_sell(s, i, price):
        if (
            i < 0
            or s.data["volume"][i] <= 0
            or not np.isfinite(s.data["down_limit"][i])
            or s.data["down_limit"][i] <= 0
        ):
            audit["exit_missing_limit_or_bar"] += 1
            return False
        if price <= s.data["down_limit"][i] + 1e-8:
            audit["exit_limit_down_deferred"] += 1
            return False
        return True

    def sell(code, i, base_price, reason, when):
        nonlocal cash, fees
        p = positions[code]
        s = stocks[code]
        scale = s.data["scale"][i]
        if not can_sell(s, i, base_price / scale):
            p["pending"] = reason
            return False
        fill = base_price * (1 - cfg.slippage)
        if fill / scale < s.data["down_limit"][i] - 1e-8:
            p["pending"] = reason
            audit["exit_slippage_limit_deferred"] += 1
            return False
        value = p["units"] * fill
        fee = cfg.fee(value, True)
        cash += value - fee
        fees += fee
        pnl = value - fee - p["cost"]
        # At least the known exit fill belongs to the excursion; the rest of an
        # intrabar exit candle is deliberately not assumed to precede the exit.
        p["peak"] = max(p["peak"], base_price)
        p["trough"] = min(p["trough"], base_price)
        trades.append(
            {
                "ts_code": code,
                "name": s.name,
                "family": p["family"],
                "signal_time": p["signal_time"],
                "entry_time": p["entry_time"],
                "exit_time": pd.Timestamp(when).isoformat(),
                "entry_price": p["raw_entry"],
                "exit_price": fill / scale,
                "initial_shares": p["shares"],
                "entry_notional": p["notional"],
                "exit_notional": value,
                "fees": p["entry_fee"] + fee,
                "pnl": pnl,
                "return_pct": pnl / p["cost"] * 100,
                "reason": reason,
                "leg": p["leg"],
                "initial_stop": p["initial_stop"],
                "exit_stop": p["stop"],
                "mfe_r": max(0, (p["peak"] - p["entry_adjusted"]) / p["initial_risk"]),
                "mae_r": min(
                    0, (p["trough"] - p["entry_adjusted"]) / p["initial_risk"]
                ),
            }
        )
        del positions[code]
        return True

    for step, end in enumerate(timeline):
        day = end.date()
        start = opening_time(end, timeframe)
        # Previously queued exits execute before this bar's entry basket.
        for code, p in list(positions.items()):
            s = stocks[code]
            i = int(s.row_at[step])
            if i < 0:
                continue
            if day > p["entry_day"]:
                op = s.data["adjusted_open"][i]
                reason = p["pending"]
                if reason is None and not cfg.close_only and op <= p["stop"]:
                    reason = (
                        "protected_stop"
                        if p["stop"] > p["initial_stop"]
                        else "stop_loss"
                    )
                elif (
                    reason is None
                    and not cfg.close_only
                    and cfg.exit_policy != "reversal"
                    and op >= p["target"]
                ):
                    reason = "take_profit"
                if reason:
                    sell(code, i, op, reason, start)
        marked = cash
        for code, p in positions.items():
            s = stocks[code]
            i = int(s.row_at[step])
            marked += p["units"] * (
                s.data["adjusted_open"][i] if i >= 0 else p["last_mark"]
            )
        for code, sig in sorted(
            orders.get(step, []), key=lambda x: (-x[1]["score"], x[0])
        ):
            key = (code, sig["family"], sig["leg"])
            if code in positions or key in consumed:
                continue
            if len(positions) >= cfg.max_positions:
                audit["capacity_rejected"] += 1
                continue
            s = stocks[code]
            i = int(s.row_at[step])
            d = s.data
            if (
                i < 1
                or d["volume"][i] <= 0
                or not np.isfinite(d["up_limit"][i])
                or not np.isfinite(d["down_limit"][i])
                or d["up_limit"][i] <= 0
                or d["down_limit"][i] <= 0
            ):
                audit["entry_missing_limit_or_bar"] += 1
                continue
            raw = d["open"][i]
            if raw >= d["up_limit"][i] - 1e-8:
                audit["entry_limit_up_rejected"] += 1
                continue
            raw_fill = raw * (1 + cfg.slippage)
            fill = raw_fill * d["scale"][i]
            risk = (fill - sig["stop"]) / fill * 100
            if (
                raw_fill > d["up_limit"][i] + 1e-8
                or not 0 < risk <= cfg.max_entry_risk_pct
            ):
                audit["gap_or_risk_rejected"] += 1
                continue
            budget = min(cash, marked * cfg.position_weight)
            if cfg.risk_budget_pct:
                budget = min(
                    budget, marked * (cfg.risk_budget_pct / 100) / (risk / 100)
                )
            quantity = (
                int(
                    min(budget / raw_fill, d["volume"][i - 1] * cfg.participation)
                    // 100
                )
                * 100
            )
            minimum = 200 if code.startswith("688") else 100
            while (
                quantity >= minimum
                and quantity * raw_fill + cfg.fee(quantity * raw_fill) > budget + 1e-8
            ):
                quantity -= 100
            if quantity < minimum:
                audit["cash_or_liquidity_rejected"] += 1
                continue
            value = quantity * raw_fill
            fee = cfg.fee(value)
            cash -= value + fee
            fees += fee
            positions[code] = {
                "units": quantity / d["scale"][i],
                "shares": quantity,
                "raw_entry": raw_fill,
                "notional": value,
                "cost": value + fee,
                "entry_fee": fee,
                "entry_day": day,
                "entry_time": start.isoformat(),
                "signal_time": s.times[sig["i"]].isoformat(),
                "entry_step": step,
                "stop": sig["stop"],
                "target": fill + (fill - sig["stop"]) * cfg.take_profit_r,
                "family": sig["family"],
                "leg": sig["leg"],
                "pending": None,
                "last_mark": fill,
                "last_seen": end.isoformat(),
                "entry_adjusted": fill,
                "initial_risk": fill - sig["stop"],
                "initial_stop": sig["stop"],
                "peak": fill,
                "trough": fill,
                "failure_count": 0,
                "line_reclaimed": False,
            }
            consumed.add(key)
            audit["entries"] += 1
        # Conditional exits cannot finance entries at this bar's earlier open.
        for code, p in list(positions.items()):
            s = stocks[code]
            i = int(s.row_at[step])
            d = s.data
            if i < 0:
                continue
            op, lo, hi = (
                d["adjusted_open"][i],
                d["adjusted_low"][i],
                d["adjusted_high"][i],
            )
            stop_hit = lo <= p["stop"]
            target_hit = cfg.exit_policy != "reversal" and hi >= p["target"]
            if cfg.close_only:
                # A close-derived decision is executable next session only,
                # including a threshold reached on the entry day (T+1).
                close = d["adjusted_close"][i]
                if not p["pending"] and close <= p["stop"]:
                    p["pending"] = (
                        "protected_stop"
                        if p["stop"] > p["initial_stop"]
                        else "stop_loss"
                    )
                elif (
                    not p["pending"]
                    and cfg.exit_policy != "reversal"
                    and close >= p["target"]
                ):
                    p["pending"] = "take_profit"
            elif day == p["entry_day"]:
                if stop_hit:
                    p["pending"] = "t1_deferred_stop"
                    audit["t1_stop_deferred"] += 1
            elif not p["pending"] and (stop_hit or target_hit):
                # Bar OHLC cannot order simultaneous touches; use the adverse one.
                reason = (
                    ("protected_stop" if p["stop"] > p["initial_stop"] else "stop_loss")
                    if stop_hit
                    else "take_profit"
                )
                price = min(op, p["stop"]) if stop_hit else max(op, p["target"])
                if stop_hit and target_hit:
                    audit["ambiguous_bracket_stop_first"] += 1
                when = (
                    start
                    if op <= p["stop"]
                    or (cfg.exit_policy != "reversal" and op >= p["target"])
                    else end
                )
                if sell(code, i, price, reason, when):
                    continue
            p["last_mark"] = d["adjusted_close"][i]
            p["last_seen"] = end.isoformat()
            p["peak"] = max(p["peak"], hi)
            p["trough"] = min(p["trough"], lo)
            if not p["pending"]:
                trail = d["trail_a" if p["family"] == "trend_pullback" else "trail_b"][
                    i
                ]
                p["failure_count"] = (
                    p["failure_count"] + 1
                    if np.isfinite(trail) and p["last_mark"] < trail
                    else 0
                )
                held = step - p["entry_step"] + 1
                if cfg.exit_policy == "reversal":
                    if np.isfinite(trail) and p["last_mark"] >= trail:
                        p["line_reclaimed"] = True
                    if not p["line_reclaimed"]:
                        p["failure_count"] = 0
                if cfg.exit_policy == "reversal":
                    from skills.quant.reversal_exit import decide

                    cost_floor = (
                        (p["cost"] + cfg.fee(p["cost"], selling=True))
                        / p["units"]
                        / (1 - cfg.slippage)
                    )
                    p["stop"], p["pending"] = decide(
                        p, p["last_mark"], d["atr"][i], held, cfg, cost_floor
                    )
                    continue
                if cfg.exit_policy == "protected":
                    # Decided at this close; the raised stop starts NEXT bar.
                    # Never apply this bar's high to this bar's earlier low.
                    atr = d["atr"][i]
                    gain_r = (p["peak"] - p["entry_adjusted"]) / p["initial_risk"]
                    if gain_r >= cfg.protect_after_r and np.isfinite(atr):
                        buffer = p["entry_adjusted"] * (
                            2 * cfg.slippage
                            + 2 * cfg.commission_rate
                            + 2 * cfg.transfer_fee
                            + cfg.stamp_tax
                        )
                        candidate = max(
                            p["entry_adjusted"] + buffer,
                            p["peak"] - cfg.trail_atr * atr,
                        )
                        p["stop"] = max(
                            p["stop"], min(candidate, p["last_mark"] - 0.1 * atr)
                        )
                    broken = (
                        held >= cfg.min_trend_holding_bars
                        and p["failure_count"] >= cfg.failure_bars
                    )
                else:
                    broken = np.isfinite(trail) and p["last_mark"] < trail
                if cfg.trend_exit and broken:
                    p["pending"] = (
                        "trend_break_confirmed"
                        if cfg.exit_policy == "protected"
                        else "trend_break"
                    )
                elif step - p["entry_step"] + 1 >= cfg.max_holding_bars:
                    p["pending"] = "holding_timeout"
        nav = cash + sum(p["units"] * p["last_mark"] for p in positions.values())
        if cash < -1e-6:
            raise AssertionError("回测出现负现金")
        if step == len(timeline) - 1 or timeline[step + 1].date() != day:
            equity.append(
                {
                    "date": day.isoformat(),
                    "equity": nav,
                    "cash": cash,
                    "positions": len(positions),
                    "exposure": (nav - cash) / nav if nav else 0,
                }
            )
    open_positions = [
        {
            "ts_code": code,
            "name": stocks[code].name,
            "family": p["family"],
            "entry_time": p["entry_time"],
            "signal_time": p["signal_time"],
            "entry_notional": p["notional"],
            "market_value": p["units"] * p["last_mark"],
            "unrealized_pnl": p["units"] * p["last_mark"] - p["cost"],
            "last_quote": p["last_seen"],
            "pending_exit": p["pending"],
        }
        for code, p in positions.items()
    ]
    metrics = summarize(equity, trades, cfg.initial_cash, len(positions))
    metrics["total_fees"] = fees
    return {
        "execution_version": "scheduled-close-next-open-v1"
        if cfg.close_only
        else "reversal-staged-exit-v2"
        if cfg.exit_policy == "reversal"
        else EXECUTION_VERSION,
        "config": asdict(cfg),
        "metrics": metrics,
        "equity": equity,
        "trades": trades,
        "open_positions": open_positions,
        "execution_audit": dict(audit),
    }
