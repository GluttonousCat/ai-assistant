# -*- encoding: utf-8 -*-
"""
Tradable range (range_trading) data API + daily scheduled scan

Routes:
  GET  /api/range/latest          Latest scan result (memory cache first, then on-disk CSV)
  GET  /api/range/dates           Trading days that have scan results
  GET  /api/range/scan/{date}     Read the scan result for a given trading day; triggers a scan if missing
  POST /api/range/scan            Manually trigger a fresh scan (runs in background)
  GET  /api/range/chart/{symbol}  Candlestick data for one symbol (with range boundaries), for the frontend popup layer

Schedule:
  A background thread fires daily at 21:30: whole-market scan -> persist + refresh cache
  (the day's daily bars are already synced by the 21:00 scheduler).
"""
from __future__ import annotations

import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

import pandas as pd
from fastapi import APIRouter, Depends, HTTPException

from core.logger import get_logger

logger = get_logger("range_trading.api")

# The range dashboard carries live trading signals; the whole route group is admin-only
# (frontend buttons are greyed out accordingly)
from api.deps import require_admin as _require_admin
range_router = APIRouter(prefix="/api/range", tags=["range"],
                         dependencies=[Depends(_require_admin)])

OUT_DIR = Path("output/range_scan")

# Memory cache: {scan_date_str: records}
_cache: Dict[str, List[Dict[str, Any]]] = {}
_cache_meta: Dict[str, Any] = {"last_scan_date": None, "last_scan_ts": None}
_lock = threading.Lock()


# ============================ Data access ============================

def _scan_csv(d: date) -> Path:
    return OUT_DIR / f"scan_{d.strftime('%Y%m%d')}.csv"


def _load_from_disk(d: date) -> Optional[List[Dict[str, Any]]]:
    p = _scan_csv(d)
    if not p.exists():
        return None
    try:
        df = pd.read_csv(p)
        return df.to_dict(orient="records")
    except Exception as e:
        logger.error(f"读取扫描结果失败 {p}: {e}")
        return None


def _available_dates() -> List[str]:
    if not OUT_DIR.exists():
        return []
    dates = []
    for f in OUT_DIR.glob("scan_*.csv"):
        tag = f.stem.replace("scan_", "")
        try:
            dates.append(datetime.strptime(tag, "%Y%m%d").date())
        except ValueError:
            continue
    return sorted({d.strftime("%Y-%m-%d") for d in dates}, reverse=True)


def _enrich_names(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Enrich records with stock names (stock_basic) + Shenwan L1/L2 industries (stock_industry -> index_classify)"""
    if not records:
        return records
    try:
        from storage.pg import PgClient
        codes = list({r["symbol"] for r in records})
        with PgClient() as pg:
            names = {r["ts_code"]: (r.get("name") or "")
                     for r in pg.fetch_all(
                         "SELECT ts_code, name FROM stock.stock_basic WHERE ts_code = ANY(%s)",
                         (codes,))}
            # L2 industries (currently effective records); L1 resolved upward via parent_code = L1.industry_code
            ind_rows = pg.fetch_all(
                """
                SELECT si.con_code,
                       l2.industry_name AS l2_name,
                       l1.industry_name AS l1_name
                FROM stock.stock_industry si
                JOIN stock.index_classify l2 ON si.index_code = l2.index_code AND l2.level = 'L2'
                LEFT JOIN stock.index_classify l1 ON l2.parent_code = l1.industry_code AND l1.level = 'L1'
                WHERE si.con_code = ANY(%s) AND si.is_new = 'Y'
                """,
                (codes,))
            industry = {r["con_code"]: (r.get("l1_name") or "", r.get("l2_name") or "")
                        for r in ind_rows}
        for r in records:
            r["name"] = names.get(r["symbol"], "")
            l1, l2 = industry.get(r["symbol"], ("", ""))
            r["industry_l1"] = l1
            r["industry_l2"] = l2
            r["sector_bias"] = _sector_bias(l1, l2)
    except Exception as e:
        logger.warning(f"补名称/行业失败: {e}")
    return records


# Sector preference weights (scheme 1: robustness first, but no added penalty on tech)
# Value/blue-chip = bonus (robust ground); tech/growth = 0 (relies on volume-shrink quality
# + market-cap filtering, no extra reward); strong-cyclical/high-position themes = mild
# penalty (avoid chasing tops)
_SECTOR_L1 = {
    "银行": 8, "食品饮料": 8, "交通运输": 7, "公用事业": 7, "非银金融": 7,
    "家用电器": 6, "建筑装饰": 5, "汽车": 5, "农林牧渔": 5, "医药生物": 4,
    "商贸零售": 4, "房地产": 3, "建筑材料": 3, "社会服务": 3,
    "电子": 0, "计算机": 0, "通信": 0, "电力设备": 0, "机械设备": 0,
    "国防军工": 0, "传媒": 0,
    "有色金属": -3, "煤炭": -4, "钢铁": -4, "石油石化": -3, "基础化工": -2,
}
_SECTOR_L2_BONUS = {
    "白酒": 3, "国有大型银行": 2, "白色家电": 2, "航空运输": 2,
}


def _sector_bias(l1: str, l2: str) -> int:
    """Sector preference score: positive = robustness bonus, 0 = neutral, negative = penalty"""
    bias = _SECTOR_L1.get(l1, 0)
    for kw, b in _SECTOR_L2_BONUS.items():
        if kw in (l2 or ""):
            bias += b
    return bias


def run_scan(scan_date: Optional[date] = None) -> Dict[str, Any]:
    """Run one whole-market scan, persist to disk + refresh cache. Returns a summary."""
    from range_trading.scanner.daily_scan import scan_market
    from storage.pg import PgClient

    df = scan_market(scan_date=scan_date, top=0, out_dir=str(OUT_DIR))
    if df.empty:
        raise RuntimeError("扫描无结果")
    # scan_market already persisted using the latest trade date internally; take that actual date
    d = scan_date
    if d is None:
        with PgClient() as pg:
            from range_trading.data.loader import get_latest_trade_date
            d = get_latest_trade_date(pg)
    key = d.strftime("%Y-%m-%d")
    records = _enrich_names(df.to_dict(orient="records"))
    with _lock:
        _cache[key] = records
        _cache_meta["last_scan_date"] = key
        _cache_meta["last_scan_ts"] = datetime.now().isoformat(timespec="seconds")
    logger.info(f"扫描完成并缓存: {key}, {len(records)} 只")

    # Persist signals + backfill outcomes of historical signals (health monitoring)
    try:
        from range_trading.research.signal_tracking import record_signals, backfill_outcomes
        with PgClient() as pg:
            record_signals(pg, d, records)
            backfill_outcomes(pg, d)
    except Exception as e:
        logger.warning(f"信号跟踪落盘/回填失败 (非致命): {e}")
    return {"date": key, "count": len(records)}


# ============================ Routes ============================

@range_router.get("/latest")
async def get_latest(min_score: float = 70.0, range_only: bool = True):
    """Latest scan result (defaults to high-quality range families only)"""
    key = _cache_meta.get("last_scan_date")
    records = None
    with _lock:
        if key and key in _cache:
            records = _cache[key]
    if records is None:
        # Memory miss -> read the latest on-disk file
        dates = _available_dates()
        if not dates:
            raise HTTPException(status_code=404, detail="尚无扫描结果, 请先 POST /api/range/scan")
        key = dates[0]
        records = _load_from_disk(datetime.strptime(key, "%Y-%m-%d").date())
        if records is None:
            raise HTTPException(status_code=404, detail=f"{key} 扫描结果读取失败")
        records = _enrich_names(records)
        with _lock:
            _cache[key] = records
            _cache_meta["last_scan_date"] = key

    data = _filter(records, min_score, range_only)
    return {
        "date": key,
        "last_scan_ts": _cache_meta.get("last_scan_ts"),
        "total": len(records),
        "count": len(data),
        "records": data,
    }


@range_router.get("/dates")
async def get_dates():
    return {"dates": _available_dates()}


@range_router.get("/scan/{scan_date}")
async def get_scan(scan_date: str, min_score: float = 70.0, range_only: bool = True):
    """Read the result for a given trading day; 404 if missing (no auto-scanning of history, avoids long blocking)"""
    try:
        d = datetime.strptime(scan_date, "%Y-%m-%d").date()
    except ValueError:
        raise HTTPException(status_code=400, detail="日期格式 YYYY-MM-DD")
    key = d.strftime("%Y-%m-%d")
    with _lock:
        records = _cache.get(key)
    if records is None:
        records = _load_from_disk(d)
        if records is None:
            raise HTTPException(status_code=404, detail=f"{key} 无扫描结果")
        records = _enrich_names(records)
        with _lock:
            _cache[key] = records
    return {"date": key, "count": len(_filter(records, min_score, range_only)),
            "records": _filter(records, min_score, range_only)}


@range_router.post("/scan")
async def trigger_scan():
    """Manually trigger the latest scan (background thread, returns immediately)"""
    def _job():
        try:
            run_scan(None)
        except Exception as e:
            logger.error(f"手动扫描失败: {e}")
    threading.Thread(target=_job, daemon=True).start()
    return {"status": "started", "message": "扫描已在后台执行, 稍后 GET /api/range/latest 查看"}


def _clean_nan(obj):
    """Recursively clean NaN/Inf floats in dicts/lists to avoid JSON serialization errors"""
    import math
    if isinstance(obj, dict):
        return {k: _clean_nan(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean_nan(v) for v in obj]
    if isinstance(obj, float) and (math.isnan(obj) or math.isinf(obj)):
        return None
    # numpy scalars
    try:
        import numpy as np
        if isinstance(obj, np.generic):
            v = obj.item()
            return None if isinstance(v, float) and (math.isnan(v) or math.isinf(v)) else v
    except Exception:
        pass
    return obj


@range_router.get("/chart/{symbol}")
async def get_chart(symbol: str, days: int = 60):
    """Candlestick data for one symbol: OHLC + 30-day percentile range + latest RangeState"""
    from storage.pg import PgClient
    from range_trading.data.loader import load_daily_bars, get_latest_trade_date
    from range_trading.regime.daily_regime import run_daily_regime

    symbol = symbol.upper()
    with PgClient() as pg:
        end = get_latest_trade_date(pg)
        start = end - timedelta(days=int(days * 2.2) + 200)
        df = load_daily_bars(pg, [symbol], start, end)
    if df.empty:
        raise HTTPException(status_code=404, detail=f"{symbol} 无数据")
    detail, state = run_daily_regime(df, symbol=symbol)

    tail = df.tail(days).reset_index(drop=True)
    candles = [
        {
            "t": r["trade_date"].strftime("%Y-%m-%d"),
            "o": round(float(r["open"]), 3), "h": round(float(r["high"]), 3),
            "l": round(float(r["low"]), 3), "c": round(float(r["close"]), 3),
            "v": round(float(r["vol"]), 1),
        }
        for _, r in tail.iterrows()
    ]
    # 30-day percentile range (same basis as the system's boundary detection)
    last30 = df.tail(30)
    upper = round(float(last30["close"].quantile(0.90)), 3)
    lower = round(float(last30["close"].quantile(0.10)), 3)

    state_dict = state.to_dict() if state else None
    if state_dict and state_dict.get("trade_date") is not None:
        td = state_dict["trade_date"]
        state_dict["trade_date"] = td.strftime("%Y-%m-%d") if hasattr(td, "strftime") else str(td)
    return _clean_nan({
        "symbol": symbol,
        "candles": candles,
        "range": {"upper": upper, "lower": lower},
        "state": state_dict,
    })


@range_router.get("/tracking")
async def get_tracking(min_score: float = 70.0, horizon: int = 10, lookback_days: int = 60):
    """Signal tracking and rolling win-rate stats (data source for the health monitoring page)"""
    from range_trading.research.signal_tracking import tracking_summary
    from storage.pg import PgClient
    try:
        with PgClient() as pg:
            result = tracking_summary(pg, min_score=min_score, horizon=horizon,
                                      lookback_days=lookback_days)
        # Enrich detail rows with names + L1/L2 industries
        result["recent_signals"] = _enrich_names(result.get("recent_signals", []))
        return result
    except Exception as e:
        logger.error(f"tracking 统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"tracking 统计失败: {e}")


# ============================ Trend system ============================

TREND_OUT_DIR = Path("output/trend_scan")


def _trend_csv(d: date) -> Path:
    return TREND_OUT_DIR / f"trend_scan_{d.strftime('%Y%m%d')}.csv"


@range_router.get("/trend/latest")
async def get_trend_latest(min_tqs: float = 0.0):
    """Latest trend scan result (reads the on-disk CSV)"""
    if not TREND_OUT_DIR.exists():
        raise HTTPException(status_code=404, detail="尚无趋势扫描结果")
    files = sorted(TREND_OUT_DIR.glob("trend_scan_*.csv"), reverse=True)
    if not files:
        raise HTTPException(status_code=404, detail="尚无趋势扫描结果")
    latest = files[0]
    tag = latest.stem.replace("trend_scan_", "")
    try:
        df = pd.read_csv(latest)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")
    if min_tqs > 0:
        df = df[df["tqs"] >= min_tqs]
    records = df.to_dict(orient="records")
    records = _enrich_names(records)
    return {
        "date": datetime.strptime(tag, "%Y%m%d").strftime("%Y-%m-%d"),
        "count": len(records),
        "records": records,
    }


@range_router.post("/trend/scan")
async def trigger_trend_scan():
    """Manually trigger a trend scan (background thread)"""
    def _job():
        try:
            from range_trading.scanner.trend_scan import scan_trend_market
            scan_trend_market(top=0, out_dir=str(TREND_OUT_DIR))
        except Exception as e:
            logger.error(f"趋势扫描失败: {e}")
    threading.Thread(target=_job, daemon=True).start()
    return {"status": "started", "message": "趋势扫描已在后台执行"}


@range_router.get("/trend/tracking")
async def get_trend_tracking(min_tqs: float = 55.0, horizon: int = 10):
    """Trend signal tracking and rolling stats (MFE/MAE/profit_ratio, trend health monitoring)"""
    from range_trading.research.trend_tracking import trend_tracking_summary
    from storage.pg import PgClient
    try:
        with PgClient() as pg:
            result = trend_tracking_summary(pg, min_tqs=min_tqs, horizon=horizon)
        result["recent_signals"] = _enrich_names(result.get("recent_signals", []))
        return result
    except Exception as e:
        logger.error(f"趋势 tracking 统计失败: {e}")
        raise HTTPException(status_code=500, detail=f"趋势 tracking 统计失败: {e}")


# ============================ Surge Setup API ============================
SETUP_OUT_DIR = Path("output/setup_scan")


def _setup_csv(d: date) -> Path:
    return SETUP_OUT_DIR / f"setup_scan_{d.strftime('%Y%m%d')}.csv"


def _setup_available_dates() -> List[str]:
    if not SETUP_OUT_DIR.exists():
        return []
    dates = []
    for f in SETUP_OUT_DIR.glob("setup_scan_*.csv"):
        tag = f.stem.replace("setup_scan_", "")
        try:
            dates.append(datetime.strptime(tag, "%Y%m%d").date())
        except ValueError:
            continue
    return sorted({d.strftime("%Y-%m-%d") for d in dates}, reverse=True)


@range_router.get("/setup/latest")
async def get_setup_latest(min_score: float = 55.0, max_score: float = 80.0,
                           detector: str = "all"):
    """Latest surge-setup candidates.
    detector: all=base shield + momentum spear / base=shield only / momentum=spear only
    Scheme 1: sector-preference weighting; scheme 2: market-gate tiering."""
    dates = _setup_available_dates()
    if not dates:
        raise HTTPException(status_code=404, detail="尚无蓄势扫描结果, 请先 POST /api/range/setup/scan")
    key = dates[0]

    # Scheme 2: market state -> dynamic minimum score (BULL lenient / RANGE neutral / BEAR strict)
    market_state, eff_min = _market_gate()
    apply_min = max(min_score, eff_min)   # take the higher of user threshold and market gate

    try:
        df = pd.read_csv(_setup_csv(datetime.strptime(key, "%Y-%m-%d").date()))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")

    # ---- detector filter ----
    if "momentum_score" in df.columns:
        if detector == "base":
            df = df[df["detector"].fillna("base") == "base"]
        elif detector == "momentum":
            df = df[df["detector"].fillna("base") == "momentum"]
    if detector == "base" or detector == "all" or "momentum_score" not in df.columns:
        df = df[(df["base_score"] >= apply_min) & (df["base_score"] < max_score)]
    records = df.to_dict(orient="records")
    records = _enrich_names(records)   # includes sector_bias
    # Add board classification + market cap
    for r in records:
        sym = r.get("symbol", "")
        if sym.endswith(".SH"):
            r["board"] = "kcb" if sym.startswith("688") else "main"
        elif sym.endswith(".SZ"):
            r["board"] = "cyb" if sym.startswith("30") else "main"
        else:
            r["board"] = "main"
        r["total_mv_yi"] = None
    try:
        from storage.pg import PgClient
        codes = [r["symbol"] for r in records]
        with PgClient() as pg:
            mv_rows = pg.fetch_all(
                "SELECT ts_code, total_mv::float8 AS mv FROM stock.daily_basic "
                "WHERE trade_date=%s AND ts_code = ANY(%s)", (datetime.strptime(key, "%Y-%m-%d").date(), codes))
        mv_map = {r["ts_code"]: r["mv"] for r in mv_rows}
        for r in records:
            mv = mv_map.get(r["symbol"])
            r["total_mv_yi"] = round(mv / 10000.0, 1) if mv else None   # 10k-yuan -> 100M-yuan
    except Exception as e:
        logger.warning(f"补市值失败: {e}")

    # Scheme 1: sort by sector-preference weighting  adjusted = base_score + sector_bias x market-cap coefficient
    for r in records:
        mv = r.get("total_mv_yi")
        bias = r.get("sector_bias", 0)
        if mv is not None:
            mv_coef = 1.0 if mv >= 100 else (0.7 if mv >= 50 else 0.4)
        else:
            mv_coef = 0.5
        r["sector_bias_adjusted"] = round(bias * mv_coef, 1)
        r["adjusted_score"] = round(r.get("base_score", 0) + bias * mv_coef, 1)
    # Sort: momentum mode by momentum-weighted score, otherwise by adjusted_score
    if detector == "momentum" and records and "m_weighted_score" in records[0]:
        records.sort(key=lambda x: x.get("m_weighted_score", 0), reverse=True)
    else:
        records.sort(key=lambda x: x.get("adjusted_score", 0), reverse=True)

    return {
        "date": key, "count": len(records), "records": _clean_nan(records),
        "market": {"state": market_state, "effective_min_score": eff_min,
                   "applied_min_score": apply_min},
    }


# Market gate tiers (scheme 2): BULL=55 / RANGE=60 / BEAR=68
_MARKET_GATE = {"BULL": 55.0, "RANGE": 60.0, "BEAR": 68.0}


def _market_gate() -> tuple:
    """Read market state and its minimum score (defaults to RANGE/60 when data is insufficient)"""
    try:
        from storage.pg import PgClient
        with PgClient() as pg:
            row = pg.fetch_one(
                "SELECT trade_date, close::float8 FROM stock.index_daily "
                "WHERE ts_code='000001.SH' ORDER BY trade_date DESC LIMIT 1")
            if not row:
                return "RANGE", 60.0
            # 20-day momentum + above the 20-day MA
            rows = pg.fetch_all(
                "SELECT close::float8 AS c FROM stock.index_daily WHERE ts_code='000001.SH' "
                "ORDER BY trade_date DESC LIMIT 21")
            closes = [r["c"] for r in rows]
            if len(closes) < 21:
                return "RANGE", 60.0
            ret20 = closes[0] / closes[20] - 1.0
            ma20 = sum(closes[:20]) / 20.0
            above = closes[0] > ma20
            if ret20 > 0.03 and above:
                return "BULL", _MARKET_GATE["BULL"]
            if ret20 < -0.03 and not above:
                return "BEAR", _MARKET_GATE["BEAR"]
            return "RANGE", _MARKET_GATE["RANGE"]
    except Exception as e:
        logger.warning(f"大盘状态读取失败, 默认 RANGE: {e}")
        return "RANGE", 60.0


@range_router.get("/setup/backtest")
async def get_setup_backtest():
    """Per-day historical backtest of the surge detector (daily_backtest_{start}_{end}_daily.csv per-day summary + main-file details)"""
    if not SETUP_OUT_DIR.exists():
        raise HTTPException(status_code=404, detail="尚无逐日回测结果")
    # Find the latest daily_backtest_*_daily.csv (per-day summary)
    daily_files = sorted(SETUP_OUT_DIR.glob("daily_backtest_*_daily.csv"), reverse=True)
    if not daily_files:
        raise HTTPException(status_code=404, detail="尚无逐日回测结果, 请先运行 setup_backtest_daily")
    try:
        by_day = pd.read_csv(daily_files[0]).to_dict(orient="records")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取失败: {e}")
    # Main file: per-signal details (date/symbol/base_score/surge/close_ret ...)
    detail = []
    main_files = sorted(SETUP_OUT_DIR.glob("daily_backtest_*.csv"), reverse=True)
    for mf in main_files:
        if "_daily" not in mf.stem:
            try:
                dfd = pd.read_csv(mf)
                dfd = dfd.sort_values("date", ascending=False).head(500)
                detail = dfd.to_dict(orient="records")
                detail = _enrich_names(detail)
            except Exception:
                pass
            break
    tag = daily_files[0].stem.replace("daily_backtest_", "").replace("_daily", "")
    return _clean_nan({"range": tag, "by_day": by_day, "detail": detail})


@range_router.get("/setup/market")
async def get_setup_market():
    """Market index guide: core indices' last ~90 days + market state verdict (bull/range/bear)"""
    from storage.pg import PgClient
    try:
        with PgClient() as pg:
            df = pg.fetch_df(
                "SELECT trade_date, ts_code, close, pct_chg::float8 FROM stock.index_daily "
                "WHERE trade_date >= (SELECT MAX(trade_date) - INTERVAL '130 days' FROM stock.index_daily) "
                "ORDER BY ts_code, trade_date")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"指数数据查询失败: {e}")
    if df.empty:
        raise HTTPException(status_code=404, detail="尚无指数数据, 请先运行 sync_index")
    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y-%m-%d")

    # Per-index series (close)
    series = {}
    for code, g in df.groupby("ts_code"):
        g = g.sort_values("trade_date")
        series[code] = [{"t": r["trade_date"], "c": round(float(r["close"]), 2)}
                        for _, r in g.iterrows()]

    # Market state verdict (SSE Composite as market proxy): 20-day/60-day momentum
    sh = df[df["ts_code"] == "000001.SH"].sort_values("trade_date").reset_index(drop=True)
    state = "UNKNOWN"
    if len(sh) > 60:
        closes = sh["close"].astype(float).values
        ret20 = closes[-1] / closes[-21] - 1
        ret60 = closes[-1] / closes[-61] - 1
        ma20 = closes[-20:].mean()
        above_ma20 = closes[-1] > ma20
        if ret20 > 0.03 and above_ma20:
            state = "BULL"       # 20-day momentum up and above the 20-day MA
        elif ret20 < -0.03 and not above_ma20:
            state = "BEAR"       # 20-day momentum down and below the 20-day MA
        else:
            state = "RANGE"      # range-bound
    latest = sh["trade_date"].iloc[-1] if len(sh) else None
    last_close = round(float(sh["close"].iloc[-1]), 2) if len(sh) else None

    state_cn = {"BULL": "强势上行", "RANGE": "震荡", "BEAR": "弱势下跌", "UNKNOWN": "未知"}.get(state, "未知")
    return {
        "latest_date": latest, "latest_close": last_close,
        "state": state, "state_cn": state_cn,
        "series": series,
    }


@range_router.post("/setup/scan")
async def trigger_setup_scan():
    """Manually trigger a surge-setup scan (background thread)"""
    def _job():
        try:
            from range_trading.scanner.setup_scan import scan_setups
            from range_trading.data.loader import get_latest_trade_date
            from storage.pg import PgClient
            with PgClient() as pg:
                d = get_latest_trade_date(pg)
                cands = scan_setups(pg, d, 55.0, 80.0, include_momentum=True)
            if not cands.empty:
                cands.to_csv(SETUP_OUT_DIR / f"setup_scan_{d.strftime('%Y%m%d')}.csv",
                             index=False, encoding="utf-8-sig")
                logger.info(f"蓄势扫描完成: {d}, {len(cands)} 只候选")
        except Exception as e:
            logger.error(f"蓄势扫描失败: {e}")
    threading.Thread(target=_job, daemon=True).start()
    return {"status": "started", "message": "蓄势扫描已在后台执行"}


def _filter(records: List[Dict[str, Any]], min_score: float, range_only: bool) -> List[Dict[str, Any]]:
    RANGE = ("RANGE_FORMATION", "EARLY_TRADABLE_RANGE", "MATURE_RANGE", "RANGE_RENEWAL")
    out = []
    for r in records:
        if range_only and r.get("state") not in RANGE:
            continue
        if r.get("range_score") is None or r["range_score"] < min_score:
            continue
        out.append(r)
    out.sort(key=lambda x: x.get("range_score", 0), reverse=True)
    return out


# ============================ Daily 21:30 schedule ============================

# Schedule: daily 21:30. The system already auto-synced the day's daily bars at
# 21:00 (core/scheduler); this job scans directly on the latest data without
# syncing again.
SCAN_HOUR = 21
SCAN_MINUTE = 30


def _daily_job():
    """Daily job: range scan + surge-setup scan + signal tracking backfill (runs in a thread pool, does not block the event loop)"""
    from datetime import date as _date
    from storage.pg import PgClient
    today = _date.today()
    logger.info(f"[调度] {SCAN_HOUR}:{SCAN_MINUTE:02d} 扫描任务触发: {today}")
    # 1. Range scan (incl. signal persistence + outcome backfill)
    try:
        run_scan(None)
    except Exception as e:
        logger.error(f"[调度] 扫描失败: {e}")
    # 2. Surge-setup scan (bottom-rebound + volume-digestion candidates, trend discovery)
    try:
        from range_trading.scanner.setup_scan import scan_setups
        from range_trading.data.loader import get_latest_trade_date
        with PgClient() as pg:
            d = get_latest_trade_date(pg)
            cands = scan_setups(pg, d, 55.0, 80.0, include_momentum=True)
        if not cands.empty:
            cands.to_csv(SETUP_OUT_DIR / f"setup_scan_{d.strftime('%Y%m%d')}.csv",
                         index=False, encoding="utf-8-sig")
            logger.info(f"[调度] 蓄势扫描完成: {d}, {len(cands)} 只候选")
        else:
            logger.info(f"[调度] 蓄势扫描完成: {d}, 无候选")
    except Exception as e:
        logger.error(f"[调度] 蓄势扫描失败: {e}")


def _seconds_until_next(hour: int = SCAN_HOUR, minute: int = SCAN_MINUTE) -> float:
    now = datetime.now()
    target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if now >= target:
        target += timedelta(days=1)
    return (target - now).total_seconds()


async def _scheduler_loop():
    """asyncio scheduler loop: fires daily at 21:30 (data already synced at 21:00); scans go to a thread pool to avoid blocking the event loop"""
    import asyncio
    logger.info(f"[调度] 每日 {SCAN_HOUR}:{SCAN_MINUTE:02d} 震荡+蓄势扫描调度已启动")
    while True:
        secs = _seconds_until_next()
        logger.info(f"[调度] 下次扫描在 {secs/3600:.2f} 小时后 (每日 {SCAN_HOUR}:{SCAN_MINUTE:02d})")
        await asyncio.sleep(secs)
        await asyncio.to_thread(_daily_job)


_scheduler_task = None


def start_scheduler():
    """Call from the FastAPI lifespan (must be inside a running event loop)"""
    import asyncio
    global _scheduler_task
    if _scheduler_task is not None and not _scheduler_task.done():
        return
    _scheduler_task = asyncio.create_task(_scheduler_loop())
    logger.info(f"每日 {SCAN_HOUR}:{SCAN_MINUTE:02d} 震荡+蓄势扫描调度任务已创建")


def catchup_if_stale():
    """
    Detect staleness at startup and catch up (handles missed timers caused by
    PC shutdown/sleep):

    If today is a trading day, it is already past 21:30, and the latest scan
    date < latest trading day, then today's (or recent days') scan did not run
    -> rerun _daily_job in the background (sync + both scans).
    Idempotent: skips directly if already up to date, no duplicate scans.
    """
    import asyncio
    from datetime import date as _date, datetime as _dt
    from storage.pg import PgClient
    from range_trading.data.loader import get_latest_trade_date
    from tools.kline.calendar import is_trading_day

    try:
        with PgClient() as pg:
            latest_td = get_latest_trade_date(pg)
    except Exception as e:
        logger.warning(f"[启动检查] 读取最新交易日失败, 跳过补跑: {e}")
        return

    # Latest scan date: memory cache first, then on-disk files
    last_scan = _cache_meta.get("last_scan_date")
    if not last_scan:
        dates = _available_dates()
        last_scan = dates[0] if dates else None

    latest_td_str = latest_td.strftime("%Y-%m-%d") if latest_td else None
    logger.info(f"[启动检查] 最新交易日={latest_td_str}, 最新扫描={last_scan}")

    if latest_td_str and last_scan == latest_td_str:
        logger.info("[启动检查] 扫描已是最新, 无需补跑")
        return

    now = _dt.now()
    # Catch up only when the data should already be ready: past 21:30 today, or the latest trading day is before today
    past_scan_time = (now.hour, now.minute) >= (SCAN_HOUR, SCAN_MINUTE)
    data_is_old = latest_td and latest_td < _date.today()
    if not (past_scan_time or data_is_old):
        logger.info("[启动检查] 尚未到当日扫描时间 (21:30), 等待定时调度")
        return

    logger.info(f"[启动检查] 检测到扫描落后 (最新扫描 {last_scan} < 最新交易日 {latest_td_str}), 后台补跑")

    def _catchup():
        # If the data itself lags, sync to latest first
        try:
            from tools.market.sync_tushare import run_backfill
            run_backfill(start=None, end=None, tables=["daily"])
        except Exception as e:
            logger.warning(f"[启动检查] 日K同步异常: {e}")
        _daily_job()

    threading.Thread(target=_catchup, daemon=True, name="range-scan-catchup").start()
