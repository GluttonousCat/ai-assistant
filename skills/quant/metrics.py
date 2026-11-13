"""Cash NAV statistics, with closed-trade and open-position results separated."""

import numpy as np


def summarize(equity, trades, initial_cash, open_count):
    nav = np.array([initial_cash] + [r["equity"] for r in equity], dtype=float)
    returns = nav[1:] / nav[:-1] - 1
    dd = nav / np.maximum.accumulate(nav) - 1
    pnl = np.array([t["pnl"] for t in trades], dtype=float)
    positive = pnl[pnl > 0].sum()
    negative = -pnl[pnl < 0].sum()
    total = float(nav[-1] / initial_cash - 1)
    active = bool(trades or open_count)
    return {
        "total_return_pct": 100 * total,
        "annualized_return_pct": 100 * ((1 + total) ** (252 / len(returns)) - 1)
        if active and len(returns) >= 20
        else None,
        "max_drawdown_pct": 100 * float(-dd.min()),
        "sharpe": float(returns.mean() / returns.std(ddof=1) * np.sqrt(252))
        if len(returns) > 1 and returns.std(ddof=1) > 1e-12
        else None,
        "closed_trades": len(trades),
        "open_positions": open_count,
        "win_rate_pct": 100 * float((pnl > 0).mean()) if len(pnl) else None,
        "profit_factor": float(positive / negative) if negative else None,
        "average_trade_return_pct": float(np.mean([t["return_pct"] for t in trades]))
        if trades
        else None,
        "realized_pnl": float(pnl.sum()),
        "ending_equity": float(nav[-1]),
        "average_exposure_pct": float(np.mean([r["exposure"] for r in equity]) * 100)
        if equity
        else 0,
    }
