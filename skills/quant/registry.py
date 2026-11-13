"""Versioned entry strategies; all reuse the dashboard's existing v4 rules."""

from skills.trend.strategy import VERSION, TrendConfig

STRATEGIES = {
    "mtf_dual_v5": {
        "name": "60F趋势·日K双机会",
        "family": None,
        "generation": "v5",
        "description": "M8/M13/M21判断60F趋势；日K转强确认入场；结构止损与有限退出方案对照，按开发段选择后固定检验。",
    },
    "mtf_pullback_v5": {
        "name": "60F趋势·日K回调",
        "family": "trend_pullback",
        "generation": "v5",
        "description": "60F方向配合已有日线升势的受控回调，日K转强后下一交易日开盘入场。",
    },
    "mtf_rebound_v5": {
        "name": "60F修复·日K反包",
        "family": "base_rebound",
        "generation": "v5",
        "description": "60F均线开始修复，日K在阴跌/盘整后反包收复；不强等日线长均线多头。",
    },
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
    from skills.trend.mtf import VERSION as MTF_VERSION, MtfConfig

    return [
        {
            **value,
            "id": key,
            "version": MTF_VERSION if value.get("generation") == "v5" else VERSION,
            "timeframes": ["1D"] if value.get("generation") == "v5" else ["60F", "1D"],
            "entry_config": (
                MtfConfig() if value.get("generation") == "v5" else TrendConfig()
            ).__dict__,
            "trend_timeframe": "60F" if value.get("generation") == "v5" else None,
        }
        for key, value in STRATEGIES.items()
    ]
