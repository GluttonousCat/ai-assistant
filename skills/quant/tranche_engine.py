"""Opt-in daily staged execution; legacy engine/results are untouched.

Buy confirmations and profit thresholds use completed bars only. All orders
execute next session. Hard exits override percentage plans. One closed campaign
is one trade; each cash fill is retained separately inside that campaign.
"""

from collections import Counter, defaultdict
from dataclasses import asdict

import numpy as np
import pandas as pd

from skills.quant.metrics import summarize
from skills.quant.registry import STRATEGIES
from skills.quant.tranches import TrancheConfig, Inventory
from skills.quant.market_context import MarketPolicy
from skills.quant.stop_orders import (
    IntradayProtection,
    completed_bar_protection,
    stop_reference,
)
from skills.quant.support_protection import SupportProtection, protected_stop

VERSION = "tranche-campaign-next-open-v1"


def simulate(
    series,
    timeline,
    strategy_id,
    cfg=TrancheConfig(),
    market_policy=None,
    stop_bars=None,
    support_protection=None,
    intraday_protection=None,
    wyckoff_policy=None,
):
    base = cfg.base
    if strategy_id == "wyckoff_60f_v31" and wyckoff_policy is None:
        raise ValueError("量价策略必须显式启用量价管理")
    if wyckoff_policy is not None and (
        not isinstance(wyckoff_policy, SupportProtection)
        or support_protection is not None
        or stop_bars is not None
    ):
        raise ValueError("量价防守需要独立的盘后支撑策略")
    if intraday_protection is not None and (
        not isinstance(intraday_protection, IntradayProtection) or stop_bars is None
    ):
        raise ValueError("盘中保护需要显式预挂策略和原生完成K线")
    if support_protection is not None and (
        not isinstance(support_protection, SupportProtection) or stop_bars is not None
    ):
        raise ValueError("支撑保护需要独立的盘后执行策略，不能混用预挂单")
    clock = pd.DatetimeIndex(timeline)
    if clock.empty or not clock.is_monotonic_increasing or clock.has_duplicates:
        raise ValueError("交易日历不合法")
    if clock.normalize().has_duplicates:
        raise ValueError("分批模型每交易日一根原生日K")
    if strategy_id not in STRATEGIES:
        raise ValueError("未知策略")
    if STRATEGIES[strategy_id].get("generation") == "rules":
        raise ValueError("组合规则首版不支持分批撮合")
    stocks = {s.code: s for s in series}
    if market_policy is not None and not isinstance(market_policy, MarketPolicy):
        raise ValueError("指数策略需要MarketPolicy")
    if len(stocks) != len(series):
        raise ValueError("重复股票")
    orders = defaultdict(list)
    audit = Counter()
    family = STRATEGIES[strategy_id]["family"]
    for s in series:
        if wyckoff_policy is not None and not {
            "wyckoff_demand_ready",
            "wyckoff_supply_warning",
            "wyckoff_supply_defense",
            "wyckoff_structure_failed",
            "confirmed_higher_low",
            "higher_low_origin",
            "higher_low_known",
        }.issubset(s.data):
            raise ValueError("缺少已完成60F量价防守与进攻证据")
        if support_protection is not None and not {
            "confirmed_higher_low",
            "higher_low_origin",
            "higher_low_known",
        }.issubset(s.data):
            raise ValueError("缺少已确认60F支撑与形成/确认时间")
        for k in ["open", "high", "low", "close"]:
            if not np.allclose(
                s.data[k] * s.data["scale"],
                s.data["adjusted_" + k],
                rtol=1e-10,
                atol=1e-8,
            ):
                raise ValueError("原始价/复权价不一致")
        if not {"scale_in_ready", "tranche_trend_failed"}.issubset(s.data):
            raise ValueError("缺少完成60F的增仓/离场证据")
        if market_policy is not None and not {
            "market_entry_allowed",
            "market_add_allowed",
            "market_context_valid",
            "market_systemic_weak",
            "market_resilient",
            "market_beta",
            "market_excess_pct",
        }.issubset(s.data):
            raise ValueError("缺少完整指数状态")
        mapping = clock.get_indexer(s.times)
        if (mapping < 0).any():
            raise ValueError("行情不在交易日历内")
        s.row_at = np.full(len(clock), -1, dtype=int)
        s.row_at[mapping] = np.arange(len(mapping))
        for sig in s.signals:
            if (family and sig["family"] != family) or (
                family is None and not sig["preferred"]
            ):
                continue
            i = sig["i"]
            audit["signals"] += 1
            if market_policy is not None and not s.data["market_entry_allowed"][i]:
                audit["market_entry_rejected"] += 1
                continue
            if i + 1 < len(mapping) and mapping[i + 1] == mapping[i] + 1:
                orders[int(mapping[i + 1])].append((s.code, sig))
            else:
                audit["no_next_bar"] += 1
    cash = base.initial_cash
    fees = 0.0
    positions = {}
    closed = []
    equity = []
    consumed = set()

    def cost_floor(p):
        inv = p["inventory"]
        return (inv.cost + base.fee(inv.cost, True)) / inv.units / (1 - base.slippage)

    def record_close(code, p, when, raw_exit):
        inv = p["inventory"]
        pnl = inv.realized
        risk = p["initial_risk"]
        entry = p["first_price"]
        t = dict(
            ts_code=code,
            name=stocks[code].name,
            family=p["family"],
            signal_time=p["signal_time"],
            entry_time=p["entry_time"],
            exit_time=when.isoformat(),
            entry_price=p["first_raw"],
            exit_price=raw_exit,
            initial_shares=p["initial_shares"],
            entry_notional=p["buy_notional"],
            exit_notional=p["sell_notional"],
            fees=p["fees"],
            pnl=pnl,
            return_pct=pnl / inv.bought_cost * 100,
            reason=p["last_reason"],
            leg=p["leg"],
            initial_stop=p["initial_stop"],
            exit_stop=p["stop"],
            mfe_r=max(0, (p["peak"] - entry) / risk),
            mae_r=min(0, (p["trough"] - entry) / risk),
            buy_legs=p["buy_stage"],
            sell_legs=sum(f["side"] == "sell" for f in p["fills"]),
            fills=p["fills"],
            excursion_basis="first_fill_price",
            entry_evidence=p.get("evidence"),
        )
        closed.append(t)
        if market_policy is not None:
            t["market_at_entry"] = p["market_at_entry"]
            t["market_grace_used"] = p["market_grace_used"]

    def sell(
        code, p, i, day, when, raw_override=None, quantity_cap=None, resolution=None
    ):
        nonlocal cash, fees
        s = stocks[code]
        d = s.data
        inv = p["inventory"]
        pending = p["pending_sell"]
        if (
            i < 0
            or d["volume"][i] <= 0
            or not np.isfinite(d["down_limit"][i])
            or d["down_limit"][i] <= 0
        ):
            audit["exit_missing_limit_or_bar"] += 1
            return
        raw = d["open"][i] if raw_override is None else raw_override
        if (
            raw <= d["down_limit"][i] + 1e-8
            or raw * (1 - base.slippage) < d["down_limit"][i] - 1e-8
        ):
            audit["exit_limit_down_deferred"] += 1
            return
        if raw * d["scale"][i] <= p["stop"] and not pending["all"]:
            pending = dict(
                all=True,
                reason="protected_stop"
                if p["stop"] > p["initial_stop"]
                else "stop_loss",
            )
            p["pending_sell"] = pending
        available = inv.available(day)
        all_exit = pending["all"]
        desired = (
            inv.units if all_exit else max(0, pending["target_sold"] - p["sold_units"])
        )
        units = min(available, desired)
        scale = d["scale"][i]
        if quantity_cap is not None:
            units = min(units, quantity_cap / scale)
        if all_exit and units >= inv.units - 1e-8:
            units = inv.units  # Full disposal includes economic odd-lot remainder.
        else:
            units = np.floor((units * scale + 1e-8) / 100) * 100 / scale
        if units <= 1e-10:
            if desired * scale < 100 and not all_exit:
                audit["partial_exit_below_lot_skipped"] += 1
                p["exit_stage"] += 1
                p["pending_sell"] = None
            else:
                audit["t1_exit_deferred"] += 1
            return
        price = raw * (1 - base.slippage) * scale
        p["peak"] = max(p["peak"], raw * scale)
        p["trough"] = min(p["trough"], raw * scale)
        value = units * price
        fee = base.fee(value, True)
        realized = inv.sell(units, value - fee, day)
        cash += value - fee
        fees += fee
        p["fees"] += fee
        p["sell_notional"] += value
        p["sold_units"] += units
        p["last_reason"] = pending["reason"]
        p["fills"].append(
            dict(
                side="sell",
                time=when.isoformat(),
                quantity=units * scale,
                price=raw * (1 - base.slippage),
                notional=value,
                fee=fee,
                pnl=realized,
                reason=pending["reason"],
            )
        )
        if support_protection is not None or wyckoff_policy is not None:
            p["fills"][-1].update(
                stop_adjusted=p["stop"],
                inventory_risk=inv.risk_at(p["stop"]),
                risk_limit=p["risk_limit"],
            )
        audit["sell_fills"] += 1
        if resolution is not None:
            p["fills"][-1]["resolution"] = resolution
            audit["resting_stop_" + resolution + "_fills"] += 1
        if inv.units <= 1e-8:
            record_close(code, p, when, raw * (1 - base.slippage))
            del positions[code]
        elif (
            not all_exit
            and pending["target_sold"] - p["sold_units"] < 100 / scale + 1e-8
        ):
            p["exit_stage"] += 1
            p["pending_sell"] = None

    def buy(code, p, i, day, when, marked, stage):
        nonlocal cash, fees
        d = stocks[code].data
        inv = p["inventory"]
        if (
            i < 1
            or d["volume"][i] <= 0
            or not all(
                np.isfinite(d[k][i]) and d[k][i] > 0 for k in ["up_limit", "down_limit"]
            )
        ):
            audit["entry_missing_limit_or_bar"] += 1
            return False
        raw = d["open"][i] * (1 + base.slippage)
        scale = d["scale"][i]
        fill = raw * scale
        if raw >= d["up_limit"][i] - 1e-8:
            audit["entry_limit_up_rejected"] += 1
            return False
        risk = (fill - p["stop"]) / fill * 100
        if not 0 < risk <= base.max_entry_risk_pct:
            audit["gap_or_risk_rejected"] += 1
            if support_protection is not None or wyckoff_policy is not None:
                audit["add_risk_rejected" if stage else "probe_risk_rejected"] += 1
            return False
        if stage and fill <= p["highest_buy_price"] + 1e-10:
            audit["averaging_down_rejected"] += 1
            return False
        if stage == 0:
            p["plan_budget"] = min(
                marked * base.position_weight, marked * base.risk_budget_pct / risk
            )
            p["risk_limit"] = marked * base.risk_budget_pct / 100
        cap = max(0, marked * base.position_weight - inv.units * fill)
        budget = min(cash, p["plan_budget"] * cfg.entry_weights[stage], cap)
        risk_cash = max(0, p["risk_limit"] - inv.risk_at(p["stop"]))
        risk_per_share = (fill - p["stop"]) / scale
        quantity = (
            int(
                min(
                    budget / raw,
                    risk_cash / risk_per_share,
                    d["volume"][i - 1] * base.participation,
                )
                // 100
            )
            * 100
        )
        while quantity >= 100:
            value = quantity * raw
            fee = base.fee(value)
            if (
                value + fee <= budget + 1e-8
                and inv.risk_at(p["stop"]) + value + fee - quantity / scale * p["stop"]
                <= p["risk_limit"] + 1e-8
            ):
                break
            quantity -= 100
        if quantity < 100:
            audit["cash_risk_or_lot_rejected"] += 1
            return False
        value = quantity * raw
        fee = base.fee(value)
        units = quantity / scale
        inv.buy(units, value + fee, day)
        cash -= value + fee
        fees += fee
        p["fees"] += fee
        p["buy_notional"] += value
        p["highest_buy_price"] = fill
        p["buy_stage"] = stage + 1
        p["last_mark"] = fill
        p["fills"].append(
            dict(
                side="buy",
                time=when.isoformat(),
                quantity=quantity,
                price=raw,
                notional=value,
                fee=fee,
                pnl=None,
                reason="probe" if not stage else "confirmed_add",
            )
        )
        if support_protection is not None or wyckoff_policy is not None:
            p["fills"][-1].update(
                stop_adjusted=p["stop"],
                inventory_risk=inv.risk_at(p["stop"]),
                risk_limit=p["risk_limit"],
            )
        if stage == 0:
            p.update(
                first_price=fill,
                first_raw=raw,
                initial_risk=fill - p["stop"],
                initial_shares=quantity,
                peak=fill,
                trough=fill,
                entry_time=when.isoformat(),
            )
        audit["buy_fills"] += 1
        return True

    for step, end in enumerate(clock):
        day = end.date()
        when = end.normalize() + pd.Timedelta(hours=9, minutes=30)
        for code, p in list(positions.items()):
            if p["pending_sell"] is not None and not p.get("resting_stop_activated"):
                sell(code, p, int(stocks[code].row_at[step]), day, when)
        marked = cash + sum(
            p["inventory"].units
            * (
                stocks[c].data["adjusted_open"][int(stocks[c].row_at[step])]
                if stocks[c].row_at[step] >= 0
                else p["last_mark"]
            )
            for c, p in positions.items()
        )
        # Existing confirmed additions precede newly initiated probes.
        for code, p in list(positions.items()):
            if p["pending_add"] is not None and not p["entry_locked"]:
                i = int(stocks[code].row_at[step])
                if i >= 0:
                    buy(code, p, i, day, when, marked, p["pending_add"])
                p["pending_add"] = None
        for code, sig in sorted(
            orders.get(step, []), key=lambda x: (-x[1]["score"], x[0])
        ):
            key = (code, sig["family"], sig["leg"])
            if code in positions or key in consumed:
                continue
            if len(positions) >= base.max_positions:
                audit["capacity_rejected"] += 1
                continue
            s = stocks[code]
            i = int(s.row_at[step])
            p = dict(
                inventory=Inventory(),
                family=sig["family"],
                leg=sig["leg"],
                signal_time=s.times[sig["i"]].isoformat(),
                entry_step=step,
                stop=sig["stop"],
                initial_stop=sig["stop"],
                plan_budget=0,
                risk_limit=0,
                fees=0.0,
                buy_notional=0.0,
                sell_notional=0.0,
                highest_buy_price=0.0,
                buy_stage=0,
                exit_stage=0,
                exit_base=None,
                sold_units=0.0,
                entry_locked=False,
                pending_add=None,
                pending_sell=None,
                fills=[],
                line_reclaimed=False,
                evidence=sig.get("evidence"),
            )
            if market_policy is not None:
                signal_i = sig["i"]
                p["market_grace_used"] = False
                p["market_at_entry"] = dict(
                    decision_time=s.times[signal_i].isoformat(),
                    beta=float(s.data["market_beta"][signal_i])
                    if np.isfinite(s.data["market_beta"][signal_i])
                    else None,
                    excess_pct=float(s.data["market_excess_pct"][signal_i])
                    if np.isfinite(s.data["market_excess_pct"][signal_i])
                    else None,
                    systemic_weak=bool(s.data["market_systemic_weak"][signal_i]),
                )
            if buy(code, p, i, day, when, marked, 0):
                positions[code] = p
                consumed.add(key)
                audit["campaigns_started"] += 1
        # Credit intraday stop proceeds only AFTER all opening acquisitions.
        # Today's new lots remain locked; the prior close's stop never tightens
        # using today's high before these native bars have been processed.
        if stop_bars is not None:
            for code, p in list(positions.items()):
                i = int(stocks[code].row_at[step])
                if i < 0:
                    continue
                bars = stop_bars.get(code, {}).get(day)
                if bars is None:
                    raise ValueError(f"{code} {day}缺少保护止损60F行情")
                scale = stocks[code].data["scale"][i]
                for bar in bars:
                    if bar.volume <= 0:
                        audit["resting_stop_liquidity_deferred"] += 1
                        continue
                    reference = stop_reference(
                        bar, p["stop"] / scale, p.get("resting_stop_activated", False)
                    )
                    if reference is None:
                        if intraday_protection is not None:
                            updated = completed_bar_protection(
                                p, bar.close * scale, cost_floor(p), intraday_protection
                            )
                            if updated > p["stop"]:
                                p["stop"] = updated
                                audit["intraday_protection_raises"] += 1
                        continue
                    if not p.get("resting_stop_activated"):
                        audit["resting_stop_activations"] += 1
                    p["resting_stop_activated"] = True
                    p["entry_locked"] = True
                    p["pending_add"] = None
                    p["pending_sell"] = dict(
                        all=True, reason="preplaced_protective_stop"
                    )
                    capacity = np.floor(bar.volume * base.participation / 100) * 100
                    if capacity < 100:
                        audit["resting_stop_liquidity_deferred"] += 1
                        continue
                    sell(
                        code,
                        p,
                        i,
                        day,
                        bar.end,
                        raw_override=reference,
                        quantity_cap=capacity,
                        resolution=bar.resolution,
                    )
                    if code not in positions:
                        break
        for code, p in positions.items():
            s = stocks[code]
            i = int(s.row_at[step])
            d = s.data
            if i < 0:
                continue
            close = d["adjusted_close"][i]
            eps = 1e-8 * d["atr"][i] if np.isfinite(d["atr"][i]) else 1e-10 * close
            p["last_mark"] = close
            p["last_seen"] = end.isoformat()
            p["peak"] = max(p["peak"], d["adjusted_high"][i])
            p["trough"] = min(p["trough"], d["adjusted_low"][i])
            inv = p["inventory"]
            held = step - p["entry_step"] + 1
            line = d["trail_a" if p["family"] == "trend_pullback" else "trail_b"][i]
            if np.isfinite(line) and close >= line:
                p["line_reclaimed"] = True
            hard_reason = None
            grace_now = False
            if close <= p["stop"]:
                hard_reason = (
                    "protected_stop" if p["stop"] > p["initial_stop"] else "stop_loss"
                )
            elif held >= base.max_holding_bars:
                hard_reason = "holding_timeout"
            elif wyckoff_policy is not None and d["wyckoff_structure_failed"][i]:
                hard_reason = "wyckoff_support_failed"
            elif (
                wyckoff_policy is None
                and base.trend_exit
                and p["line_reclaimed"]
                and held >= base.min_trend_holding_bars
                and d["tranche_trend_failed"][i]
            ):
                hard_reason = "structure_failed"
            elif (
                wyckoff_policy is None
                and p["family"] == "base_rebound"
                and held >= base.rebound_patience
                and p["peak"] - p["first_price"]
                < base.rebound_min_progress_r * p["initial_risk"]
                and close < p["first_price"]
            ):
                hard_reason = "rebound_failed"
            if hard_reason:
                if (
                    hard_reason == "structure_failed"
                    and market_policy is not None
                    and market_policy.soft_grace_days
                    and not p["market_grace_used"]
                    and d["market_context_valid"][i]
                    and d["market_systemic_weak"][i]
                    and d["market_resilient"][i]
                ):
                    # One observation session per campaign; no repeated excuses
                    # for a continuing decline. Price stops above always win.
                    rebound_failed = (
                        p["family"] == "base_rebound"
                        and held >= base.rebound_patience
                        and p["peak"] - p["first_price"]
                        < base.rebound_min_progress_r * p["initial_risk"]
                        and close < p["first_price"]
                    )
                    if rebound_failed:
                        hard_reason = "rebound_failed"
                    else:
                        hard_reason = None
                        grace_now = True
                        p["market_grace_used"] = True
                        p["pending_add"] = None
                        audit["systemic_soft_grace"] += 1
            if hard_reason:
                p["entry_locked"] = True
                p["pending_add"] = None
                p["pending_sell"] = dict(all=True, reason=hard_reason)
                continue
            if p["pending_sell"] is not None:
                continue
            if support_protection is not None or wyckoff_policy is not None:
                updated = protected_stop(
                    p,
                    close,
                    d["atr"][i],
                    d["confirmed_higher_low"][i],
                    d["higher_low_origin"][i],
                    d["higher_low_known"][i],
                    end,
                    bool(
                        d["wyckoff_demand_ready"][i]
                        if wyckoff_policy is not None
                        else d["scale_in_ready"][i]
                    ),
                    wyckoff_policy or support_protection,
                )
                if updated > p["stop"]:
                    p["stop"] = updated
                    audit["support_stop_raises"] += 1
            if (
                wyckoff_policy is not None
                and d["wyckoff_supply_defense"][i]
                and not p.get("supply_defense_used")
                and p["exit_stage"] < len(cfg.exit_weights) - 1
            ):
                if p["exit_base"] is None:
                    p["exit_base"] = inv.units
                    p["profit_anchor"] = inv.cost / inv.units
                p["supply_defense_used"] = True
                p["entry_locked"] = True
                p["pending_add"] = None
                p["pending_sell"] = dict(
                    all=False,
                    target_sold=p["exit_base"]
                    * sum(cfg.exit_weights[: p["exit_stage"] + 1]),
                    reason="wyckoff_supply_defense",
                )
                audit["wyckoff_defensive_orders"] += 1
                continue
            anchor = (
                p["profit_anchor"]
                if p["exit_base"] is not None
                else inv.cost / inv.units
            )
            if (
                p["exit_stage"] < len(cfg.exit_weights) - 1
                and (close / anchor - 1) * 100 + 1e-8
                >= cfg.take_profit_pct[p["exit_stage"]]
            ):
                if p["exit_base"] is None:
                    p["exit_base"] = inv.units
                    p["profit_anchor"] = anchor
                p["entry_locked"] = True
                p["pending_add"] = None
                p["pending_sell"] = dict(
                    all=False,
                    target_sold=p["exit_base"]
                    * sum(cfg.exit_weights[: p["exit_stage"] + 1]),
                    reason=f"tranche_profit_{p['exit_stage'] + 1}",
                )
            elif (
                not p["entry_locked"]
                and not grace_now
                and p["buy_stage"] < len(cfg.entry_weights)
                and d["scale_in_ready"][i]
                and (wyckoff_policy is None or not d["wyckoff_supply_warning"][i])
                and (market_policy is None or d["market_add_allowed"][i])
                and close > p["highest_buy_price"] + eps
                and close - p["first_price"] + eps
                >= cfg.add_at_r[p["buy_stage"] - 1] * p["initial_risk"]
            ):
                p["pending_add"] = p["buy_stage"]
            if (close / anchor - 1) * 100 >= cfg.protect_at_pct:
                atr = d["atr"][i]
                if np.isfinite(atr) and atr > 0:
                    candidate = cost_floor(p)
                    if np.isfinite(line):
                        candidate = max(candidate, line - 0.5 * atr)
                    p["stop"] = max(p["stop"], min(candidate, close - 0.1 * atr))
        nav = cash + sum(
            p["inventory"].units * p["last_mark"] for p in positions.values()
        )
        if cash < -1e-6:
            raise AssertionError("分批账户出现负现金")
        equity.append(
            dict(
                date=day.isoformat(),
                equity=nav,
                cash=cash,
                positions=len(positions),
                exposure=(nav - cash) / nav if nav else 0,
            )
        )
    opened = [
        dict(
            ts_code=c,
            name=stocks[c].name,
            family=p["family"],
            entry_time=p["entry_time"],
            signal_time=p["signal_time"],
            entry_notional=p["buy_notional"],
            market_value=p["inventory"].units * p["last_mark"],
            unrealized_pnl=p["inventory"].units * p["last_mark"] - p["inventory"].cost,
            partial_realized_pnl=p["inventory"].realized,
            last_quote=p["last_seen"],
            pending_exit=p["pending_sell"]["reason"] if p["pending_sell"] else None,
            buy_legs=p["buy_stage"],
            sell_legs=sum(f["side"] == "sell" for f in p["fills"]),
            fills=p["fills"],
        )
        for c, p in positions.items()
    ]
    metrics = summarize(equity, closed, base.initial_cash, len(opened))
    metrics.update(
        total_fees=fees,
        realized_pnl=sum(t["pnl"] for t in closed)
        + sum(p["partial_realized_pnl"] for p in opened),
        buy_fills=audit["buy_fills"],
        sell_fills=audit["sell_fills"],
    )
    config = {
        **asdict(base),
        **{k: v for k, v in asdict(cfg).items() if k != "base"},
        "exit_policy": "tranches",
    }
    if market_policy is not None:
        config["market_policy"] = asdict(market_policy)
        metrics["systemic_soft_grace_uses"] = audit["systemic_soft_grace"]
        for item in opened:
            item["market_at_entry"] = positions[item["ts_code"]]["market_at_entry"]
            item["market_grace_used"] = positions[item["ts_code"]]["market_grace_used"]
    if stop_bars is not None:
        config["stop_execution"] = "preplaced_ohlc_volume_capped"
    if support_protection is not None:
        config["support_protection"] = asdict(support_protection)
    if intraday_protection is not None:
        config["intraday_protection"] = asdict(intraday_protection)
    if wyckoff_policy is not None:
        config["wyckoff_policy"] = asdict(wyckoff_policy)
    return dict(
        execution_version="tranche-wyckoff-after-close-v5"
        if wyckoff_policy is not None
        else "tranche-support-after-close-v4"
        if support_protection is not None
        else "tranche-native60f-stop-v3"
        if stop_bars is not None
        else "tranche-market-aware-v2"
        if market_policy is not None
        else VERSION,
        config=config,
        metrics=metrics,
        equity=equity,
        trades=closed,
        open_positions=opened,
        execution_audit=dict(audit),
    )
