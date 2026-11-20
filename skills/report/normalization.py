"""Pure report metadata normalization, shared by streaming and batch extraction."""

import json
import math
import re
from datetime import date

from core.helpers import normalize_org_name
from skills.report.attribution import source_location
from skills.report.extraction import list_items, saved_analysis_version, text_items
from typing import Any, Dict, List, Optional

MARKETS = {
    "A股",
    "H股",
    "台股",
    "TW股",
    "日股",
    "韩股",
    "美股",
    "欧洲股",
    "东南亚股",
    "宏观",
    "商品",
    "行业",
    "其他",
}


_METRIC_LABELS = {
    "revenue": "营业收入", "net_profit": "净利润", "eps": "每股收益",
    "roe": "净资产收益率", "target_price": "目标价", "gross_margin": "毛利率",
}


_RATING_WHITELIST = {
    "买入",
    "增持",
    "推荐",
    "持有",
    "中性",
    "卖出",
    "跑赢大盘",
    "跑输大盘",
    "无",
    "",
}


def join_tags(items: Any, limit: int = 6) -> str:
    """Tag list -> comma-separated string (dedupe/drop empties/truncate)"""
    if not isinstance(items, list):
        return ""
    seen: List[str] = []
    for it in items:
        t = str(it).strip()[:24]
        if t and t not in seen:
            seen.append(t)
        if len(seen) >= limit:
            break
    return ",".join(seen)


def display_title(org: Optional[str], orig_name: str) -> str:
    """
    Build the 'broker-original file name' display title.
    - the broker comes from the LLM-extracted org_name (missing -> leave the title alone);
    - the original file name is kept as-is (minus any .pdf suffix);
    - if the file name already starts with the broker, do not prepend it again.
    """
    org = str(org or "").strip()
    name = (orig_name or "").strip()
    if name.lower().endswith(".pdf"):
        name = name[:-4]
    if not org or not name:
        return (name or orig_name or "")[:200]
    if name.startswith(org) or name.startswith(f"中文版-{org}"):
        return name[:200]
    return f"{org}-{name}"[:200]


def normalize_codes(codes: List[Any]) -> List[str]:
    out = []
    for c in list_items(codes):
        c = str(c).strip().upper()
        if re.match("^\\d{6}\\.(SH|SZ|BJ)$", c) and c not in out:
            out.append(c)
    return out


def normalize_symbols(symbols: List[Any]) -> List[str]:
    """Non-A-share ticker symbols (NVDA.O / 0981.HK / BABA etc.)"""
    out = []
    for s in list_items(symbols):
        s = str(s).strip().upper()
        if re.match("^[A-Z0-9.\\-]{2,12}$", s) and s not in out:
            out.append(s)
    return out[:10]


def normalize_forecasts(
    forecasts: List[Any], ts_codes: List[str], market: str, symbols: List[str], *, attributed=False
) -> tuple[List[Dict[str, Any]], int]:
    by_key = {}
    conflicts = set()
    for f in list_items(forecasts, 1200):
        if not isinstance(f, dict):
            continue
        metric = str(f.get("metric") or "").strip()
        if metric not in _METRIC_LABELS:
            continue
        try:
            value = float(f.get("value"))
        except (OverflowError, TypeError, ValueError):
            continue
        if isinstance(f.get("value"), bool) or not math.isfinite(value) or abs(value) >= 1e20:
            continue
        unit = str(f.get("unit") or "").strip()
        period = "NA" if metric == "target_price" else str(f.get("period") or "").strip().upper()
        if not unit or not period or len(unit) > 16 or len(period) > 16:
            continue
        symbol = str(f.get("symbol") or "").strip().upper()
        ts_code = None
        if re.match("^\\d{6}\\.(SH|SZ|BJ)$", symbol):
            ts_code, symbol = (symbol, None)
        elif symbol and not normalize_symbols([symbol]):
            continue
        elif not symbol:
            if len(ts_codes) == 1:
                ts_code = ts_codes[0]
            elif len(symbols) == 1:
                symbol = symbols[0]
            elif market == "A股":
                continue
        try:
            confidence = float(f.get("confidence", 80))
        except (OverflowError, TypeError, ValueError):
            confidence = 80.0
        forecast = {
            "ts_code": ts_code, "symbol": symbol or None, "market": market,
            "metric": metric, "period": period, "value": value, "unit": unit,
            "confidence": confidence if 0 <= confidence <= 100 else 80.0,
            "raw_text": str(f.get("raw_text") or "")[:500],
        }
        if isinstance(f.get("source"), dict):
            forecast["source"] = f["source"]
        key = ts_code, symbol, metric, period
        if attributed:
            if not source_location(f.get("source")):
                continue
            if source_location(f.get("header_source")):
                forecast["header_source"] = f["header_source"]
            forecast["attribution"] = f.get("attribution") or "unknown"
            forecast["stance"] = f.get("stance") or "unknown"
            shown = int(value) if value.is_integer() else value
            horizon = "" if period == "NA" else f"{period} "
            forecast["raw_text"] = f"{horizon}{_METRIC_LABELS[metric]} {shown}{unit}"
            forecast["confidence"] = None
            # Management guidance and a broker estimate for the same period
            # are separate observations even when their values differ.
            key += (forecast["attribution"], forecast["stance"])
        prior = by_key.setdefault(key, forecast)
        if (prior["value"], prior["unit"]) != (value, unit):
            conflicts.add(key)
    return [forecast for key, forecast in by_key.items() if key not in conflicts], len(conflicts)


def prepare_extraction(row, data):
    """Normalize a model result into metadata, forecasts and the UI summary."""
    market = str(data.get("market") or row.get("market") or "其他").strip()
    market = {"台股": "TW股", "HK股": "H股"}.get(market, market)
    if market not in MARKETS:
        market = row.get("market") if row.get("market") in MARKETS else "其他"
    codes = normalize_codes(data.get("ts_codes") or [])
    symbols = normalize_symbols(data.get("symbols") or [])
    commodities = text_items(data.get("commodities"), 10, 32)
    tags = data.get("tags") if isinstance(data.get("tags"), dict) else {}
    attributed = saved_analysis_version(data) >= 4
    rating = str(data.get("rating") or "无")
    if rating not in _RATING_WHITELIST or (attributed and not source_location(data.get("rating_source"))):
        rating = "无"
    forecasts, conflicts = normalize_forecasts(data.get("forecasts"), codes, market, symbols, attributed=attributed)
    coverage = data.get("coverage")
    if isinstance(coverage, dict):
        coverage = {**coverage, "forecast_conflicts": conflicts}
    canonical = {
        **data, "market": market, "ts_codes": codes, "symbols": symbols,
        "rating": rating, "commodities": commodities, "tags": tags,
        "forecasts": [
            {**forecast, "symbol": forecast["ts_code"] or forecast["symbol"]}
            for forecast in forecasts
        ],
    }
    if coverage is not None:
        canonical["coverage"] = coverage
    fields = {
        "analysis_json": json.dumps(canonical, ensure_ascii=False),
        "market": market,
        "tags_industries": join_tags(tags.get("industries")),
        "tags_regions": join_tags(tags.get("regions")),
        "tags_commodities": join_tags(tags.get("commodities")) or ",".join(commodities),
        "tags_themes": join_tags(tags.get("themes")),
        "tags_targets": join_tags(tags.get("targets"))
        or ",".join(text_items(data.get("company_names"), 5, 32)),
    }
    if codes and not row.get("ts_code") and not row.get("symbols"):
        fields["ts_code"] = codes[0]
    if symbols and not row.get("symbols"):
        fields["symbols"] = ",".join(symbols)
    for key, length in (("report_type", 16), ("author", 64)):
        if data.get(key) and not row.get(key):
            fields[key] = str(data[key])[:length]
    published = str(data.get("publish_date") or "").strip()[:10]
    if published and not row.get("publish_date"):
        try:
            fields["publish_date"] = date.fromisoformat(published).isoformat()
        except ValueError:
            pass
    if data.get("org_name") and not row.get("org_name"):
        fields["org_name"] = normalize_org_name(str(data["org_name"]))[:64]
    title = row.get("title")
    if not title or title == row.get("file_name"):
        title = display_title(
            (row.get("org_name") or "").strip() or normalize_org_name(data.get("org_name")),
            row.get("file_name") or title or "",
        )
    if title and title != row.get("title"):
        fields["title"] = title[:200]
    summary = {
        "analysis_version": data.get("analysis_version"),
        "summary_evidence": canonical.get("summary_evidence"),
        "forecast_excerpts": canonical.get("forecast_excerpts") or [],
        "rating_source": canonical.get("rating_source"),
        "material_type": canonical.get("material_type"),
        "material_source": canonical.get("material_source"),
        "report_id": row["report_id"],
        "title": title or row.get("title"),
        "market": market,
        "rating": rating,
        "report_type": str(data.get("report_type") or "")[:16],
        "ts_code": codes[0] if codes else None,
        "symbols": symbols,
        "org_name": str(data.get("org_name") or row.get("org_name") or "")[:64],
        "core_view": canonical.get("core_view") or "",
        "key_points": canonical.get("key_points") or [],
        "risks": canonical.get("risks") or [],
        "evidence": data.get("evidence") or [],
        "research_insights": canonical.get("research_insights") or {},
        "coverage": coverage,
        "forecast_count": len(forecasts),
        "forecasts": forecasts,
    }
    # Keep secondhand numbers visible, without counting another broker forecast.
    if attributed:
        broker_forecasts = [f for f in forecasts if data.get("material_type") == "original"
                            and f["attribution"] == "broker" and f["stance"] == "expectation"]
    else:
        broker_forecasts = [] if data.get("material_type") == "commentary" else forecasts
    summary["broker_forecast_count"] = len(broker_forecasts)
    return fields, broker_forecasts, summary
