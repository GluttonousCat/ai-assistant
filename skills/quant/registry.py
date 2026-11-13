"""Versioned entry strategies; all reuse the dashboard's existing v4 rules."""

from skills.trend.strategy import VERSION, TrendConfig

STRATEGIES = {
    "trend_dual_v4": {
        "name": "趋势双机会",
        "family": None,
        "description": "原升势中的回调再起，以及横盘阴跌/砸坑后的结构修复；首阳观察，第2—4推进确认。",
    },
    "trend_pullback_v4": {
        "name": "趋势回调介入",
        "family": "trend_pullback",
        "description": "已有升势与受控回调，止跌后重新推进；60F要求已完成日线方向向上。",
    },
    "base_rebound_v4": {
        "name": "阴跌/砸坑反弹",
        "family": "base_rebound",
        "description": "横盘阴跌修复或坑后收回原平台；允许日线方向仍偏弱。",
    },
}


def catalog():
    return [
        {
            **value,
            "id": key,
            "version": VERSION,
            "timeframes": ["60F", "1D"],
            "entry_config": TrendConfig().__dict__,
        }
        for key, value in STRATEGIES.items()
    ]
