"""Pure calculations for the four Alpha quadrants; no persistence or LLM IO."""

import json
from typing import Dict, List, Optional
import pandas as pd

_UNIT_FACTOR = {
    "亿元": 100000000.0,
    "万亿元": 1000000000000.0,
    "万元": 10000.0,
    "百万元": 1000000.0,
    "亿元/年": 100000000.0,
    "元": 1.0,
    "%": 1.0,
    "倍": 1.0,
    "倍数": 1.0,
}


def to_yuan(value: float, unit: Optional[str]) -> float:
    """Normalize a forecast value to yuan by its unit (testable pure function)"""
    return float(value) * _UNIT_FACTOR.get((unit or "").strip(), 1.0)


def classify_drift(
    first: Optional[float], last: Optional[float], tol: float = 3.0
) -> Optional[str]:
    """First-half vs second-half forecast means -> upward/downward/flat revision
    (within +/-tol% counts as flat; None when data is insufficient)"""
    if first is None or last is None or first == 0:
        return None
    drift = (last - first) / abs(first) * 100
    if drift > tol:
        return "上修"
    if drift < -tol:
        return "下修"
    return "持平"


def edge_trend(
    latest: Optional[float], prev: Optional[float], diff_pp: float = 5.0
) -> Optional[str]:
    """
    Marginal direction of YoY between adjacent report periods (pure function,
    verified by tests).
    The signal carries growth-level context so high growth is not mislabeled
    as "decelerating":
    - high level (>=50%): high-level accelerating / high-level decelerating / high-level steady
    - negative growth (<0%): negative narrowing / negative widening / negative flat
    - otherwise: accelerating / decelerating / steady
    (The returned labels themselves are Chinese terms like 高位提速/负增收窄/提速.)
    """
    if latest is None or prev is None:
        return None
    diff = latest - prev
    if latest < 0:
        if diff >= diff_pp:
            return "负增收窄"
        if diff <= -diff_pp:
            return "负增扩大"
        return "负增持平"
    base = "提速" if diff >= diff_pp else "降速" if diff <= -diff_pp else "平稳"
    return f"高位{base}" if latest >= 50 else base


def forecast_divergence(rows) -> List[Dict]:
    """Quadrant 1: forecast divergence and direction (per metric per period,
    normalized by unit before statistics)"""
    if not rows:
        return []
    df = pd.DataFrame(rows)
    df["value"] = df.apply(
        lambda r: to_yuan(r["forecast_value"], r["forecast_unit"]), axis=1
    )
    # Compare scaled values only when they describe the same unit and currency.
    df["unit"] = df["forecast_unit"].fillna("").str.strip().replace({
        "万亿元": "元", "亿元": "元", "百万元": "元", "万元": "元",
        "亿元/年": "元/年", "倍数": "倍",
    })
    out = []
    for (ftype, period, unit), g in df.groupby(["forecast_type", "forecast_period", "unit"]):
        if not unit or len(g) < 2:
            continue
        vals = g["value"]
        mean = float(vals.mean())
        cv = float(vals.std(ddof=1) / abs(mean)) if mean and len(vals) >= 2 else None
        half = max(1, len(g) // 2)
        first, last = (float(vals.iloc[:half].mean()), float(vals.iloc[-half:].mean()))
        direction = classify_drift(first, last)
        out.append(
            {
                "forecast_type": ftype,
                "forecast_period": str(period),
                "unit": unit,
                "n_forecasts": int(len(g)),
                "n_orgs": int(g["org_name"].nunique()),
                "mean": mean,
                "cv": round(cv, 3) if cv is not None else None,
                "direction": direction,
                "drift_pct": round((last - first) / abs(first) * 100, 1)
                if first
                else None,
            }
        )
    out.sort(key=lambda x: x["forecast_period"], reverse=True)
    return out[:6]


def financial_momentum(rows) -> List[Dict]:
    """Quadrant 2: financial momentum (last 8 report periods) + marginal-direction markers"""
    if not rows:
        return []
    df = pd.DataFrame(rows)
    for c in ("or_yoy", "netprofit_yoy", "grossprofit_margin", "q_sales_yoy"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df = df.sort_values("end_date")
    recs = df.to_dict("records")
    if len(recs) >= 2:
        last, prev = (recs[-1], recs[-2])
        trend = {
            "or_yoy_trend": edge_trend(last.get("or_yoy"), prev.get("or_yoy")),
            "netprofit_yoy_trend": edge_trend(
                last.get("netprofit_yoy"), prev.get("netprofit_yoy")
            ),
            "q_sales_yoy_trend": edge_trend(
                last.get("q_sales_yoy"), prev.get("q_sales_yoy")
            ),
        }
        gm = df["grossprofit_margin"].dropna()
        trend["gm_trend"] = (
            "上行"
            if len(gm) >= 4 and gm.iloc[-2:].mean() > gm.iloc[:-2].mean()
            else "下行"
            if len(gm) >= 4
            else None
        )
        recs[-1]["trend"] = trend
    for r in recs:
        r["end_date"] = str(r["end_date"])
        for k, v in list(r.items()):
            if isinstance(v, float) and pd.notna(v):
                r[k] = round(v, 2)
    return recs


def valuation_context(rows) -> Dict:
    """Quadrant 3: PE/PB percentile over the last 3 years (0=lowest 100=highest)"""
    if not rows:
        return {}

    def _pct(vals: List, key: str) -> Optional[Dict]:
        arr = [
            float(r[key]) for r in vals if r.get(key) is not None and float(r[key]) > 0
        ]
        if len(arr) < 60:
            return None
        latest = arr[-1]
        pct = sum((1 for v in arr if v < latest)) / len(arr) * 100
        return {"latest": round(latest, 2), "pct_rank": round(pct, 1)}

    return {"pe_ttm": _pct(rows, "pe_ttm"), "pb": _pct(rows, "pb"), "n_days": len(rows)}


def report_views(rows) -> List[Dict]:
    """Quadrant 4: recent report views (three-way match on target binding/title/tags_targets)"""
    out = []
    for r in rows:
        rating = core_view = None
        material_type = "unknown"
        try:
            aj = json.loads(r["analysis_json"] or "{}")
            rating = aj.get("rating")
            material_type = aj.get("material_type") or "unknown"
            cv = aj.get("core_view") or aj.get("summary")
            core_view = str(cv)[:120] + "…" if cv and len(str(cv)) > 120 else cv
        except Exception:
            pass
        out.append(
            {
                "title": r["title"],
                "org": r["org_name"],
                "date": str(r["publish_date"]),
                "rating": rating,
                "material_type": material_type,
                "core_view": core_view,
            }
        )
    return out
