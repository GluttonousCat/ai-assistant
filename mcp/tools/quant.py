# -*- encoding: utf-8 -*-
"""
量化/技术域工具 (2个):
- compute_indicators   技术指标计算 (ADX / POC / Wyckoff Spring)
- run_quant_scan       range_trading 扫描 (单标的 regime / 全市场震荡 / 趋势 / 蓄势盾)
"""
from __future__ import annotations

from typing import Any, Dict, List

import pandas as pd

from mcp.registry import REGISTRY
from mcp.spec import ToolError, obj_schema, param
from mcp.tools._common import _jsonable, df_payload, load_daily_bars, resolve_one


# ============================================================
# 15. compute_indicators
# ============================================================

@REGISTRY.tool(
    name="compute_indicators",
    domain="quant",
    description=(
        "计算个股**技术指标数值**: ADX(14) 趋势强度, POC(60日量能重心), "
        "Wyckoff Spring 信号 (跌破20日支撑后收回+弱趋势+低量位置)。"
        "用户问 'XX趋势强不强'/'XX有没有弹簧信号'/'量能重心在哪' 时调用。"
        "区别: 问当前处于什么**形态/区间状态** (状态机判定) 用 run_quant_scan 的 regime。"
        "返回最新值与近 5 日演变; 只算指标不给买卖建议。"),
    params_schema=obj_schema({
        "stock": param("股票名或代码", "string"),
        "days": param("回看交易日数 (>=150)", "integer", default=250),
    }, ["stock"]),
    examples=["贵州茅台趋势强不强", "中际旭创有没有Wyckoff信号", "宁德的量能重心在哪"],
)
def compute_indicators(stock: str, days: int = 250) -> Dict[str, Any]:
    hit = resolve_one(stock)
    df = load_daily_bars(hit["ts_code"], days=max(150, int(days)))
    # indicators.py 约定: 列名 volume; index 为 DatetimeIndex (信号函数会取 index.date())
    df = df.rename(columns={"vol": "volume"})
    df.index = pd.DatetimeIndex(pd.to_datetime(df["trade_date"]))
    from tools.kline.indicators import (
        calculate_adx, calculate_rolling_poc, generate_wyckoff_signals_adx_vp,
    )
    out = df.copy()
    out["adx"] = calculate_adx(out)
    out["poc"] = calculate_rolling_poc(out)
    signals = generate_wyckoff_signals_adx_vp(out)
    sig_col = next((c for c in ("signal_adx_vp", "wyckoff_spring")
                    if c in signals.columns), signals.columns[-1])

    latest = out.iloc[-1]
    recent = out.tail(5)[["trade_date", "close", "adx", "poc"]]
    recent_sig = signals.tail(10)
    return {
        "stock": hit, "as_of": _jsonable(latest["trade_date"]),
        "latest": {"close": round(float(latest["close"]), 2),
                   "adx": round(float(latest["adx"]), 1),
                   "poc": round(float(latest["poc"]), 2)},
        "adx_hint": "ADX<20 弱趋势/震荡, 20-40 趋势形成, >40 强趋势",
        "recent_5d": [{k: _jsonable(v) for k, v in r.items()}
                      for r in recent.to_dict("records")],
        "wyckoff_spring_recent": bool(recent_sig[sig_col].any())
        if sig_col in recent_sig else False,
    }


# ============================================================
# 16. run_quant_scan
# ============================================================

@REGISTRY.tool(
    name="run_quant_scan",
    domain="quant",
    description=(
        "range_trading 量化扫描, 四种类型: "
        "regime=单标的区间**形态状态** (8态状态机快照, 秒级, 最常用); "
        "range=全市场震荡区间扫描排名 (慢, 分钟级); "
        "trend=全市场趋势行情扫描 (慢); "
        "setup=蓄势盾形态候选 (慢)。"
        "用户问 'XX处于什么形态/区间' 用 regime; 只要 ADX/POC 等指标数值用 "
        "compute_indicators; '今天扫一下全市场' 才用 range/trend/setup。"
        "全市场扫描较重, 结果同时落盘 output/ 目录。"),
    params_schema=obj_schema({
        "scan_type": param("扫描类型", "string",
                           enum=["regime", "range", "trend", "setup"],
                           default="regime"),
        "stock": param("regime 类型的目标股票", "string"),
        "top": param("全市场扫描返回前 N 名", "integer", default=20),
    }, ["scan_type"]),
    examples=["中际旭创现在什么形态", "今天全市场震荡扫描", "趋势扫描前20"],
    notes="range/trend/setup 为全市场重扫描 (分钟级); regime 秒级",
)
def run_quant_scan(scan_type: str = "regime", stock: str = "",
                   top: int = 20) -> Dict[str, Any]:
    if scan_type == "regime":
        if not stock:
            raise ToolError("regime 类型必须提供 stock")
        hit = resolve_one(stock)
        from storage.pg import PgClient
        from range_trading.data.loader import (
            estimate_start_date, get_latest_trade_date, load_daily_bars,
        )
        from range_trading.regime.daily_regime import run_daily_regime
        with PgClient() as pg:
            as_of = get_latest_trade_date(pg)
            df = load_daily_bars(pg, [hit["ts_code"]],
                                 estimate_start_date(as_of), as_of)
        if df.empty:
            raise ToolError(f"{hit['name']} 无日K数据")
        _, state = run_daily_regime(df, symbol=hit["ts_code"])
        if state is None:
            raise ToolError(f"{hit['name']} 日K历史不足, 无法生成区间状态")
        from dataclasses import asdict
        return {"stock": hit, "as_of": str(as_of),
                "regime": {k: _jsonable(v) for k, v in asdict(state).items()},
                "state_hint": {
                    "RANGE_FORMATION": "区间形成", "RANGE_EXTENSION": "区间扩展",
                    "RANGE_REVERSAL_UP": "区间内反转上行",
                    "RANGE_REVERSAL_DOWN": "区间内反转下行",
                    "RANGE_BREAKOUT_UP": "区间突破上行",
                    "RANGE_BREAKOUT_DOWN": "区间突破下行",
                }.get(state.state, state.state)}

    if scan_type == "range":
        from range_trading.scanner.daily_scan import scan_market
        df = scan_market(top=min(int(top), 50))
        out = df_payload(df, max_rows=int(top))
        return {"scan_type": scan_type, **out}

    if scan_type == "trend":
        from range_trading.scanner.trend_scan import scan_trend_market
        df = scan_trend_market(top=min(int(top), 50))
        out = df_payload(df, max_rows=int(top))
        return {"scan_type": scan_type, **out}

    if scan_type == "setup":
        from storage.pg import PgClient
        from range_trading.data.loader import get_latest_trade_date
        from range_trading.scanner.setup_scan import scan_setups
        with PgClient() as pg:
            scan_date = get_latest_trade_date(pg)
            cands = scan_setups(pg, scan_date)
        out = df_payload(cands, max_rows=int(top))
        return {"scan_type": scan_type, "scan_date": str(scan_date), **out}

    raise ToolError(f"未知 scan_type: {scan_type}")
