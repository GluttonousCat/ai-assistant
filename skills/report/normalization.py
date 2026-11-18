"""Pure report metadata normalization, shared by streaming and batch extraction."""

import json
import re

from core.helpers import normalize_org_name
from typing import Any, Dict, List, Optional

MARKETS = {
    "A股",
    "H股",
    "台股",
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


_METRIC_WHITELIST = {
    "revenue",
    "net_profit",
    "eps",
    "roe",
    "target_price",
    "gross_margin",
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
    for c in codes:
        c = str(c).strip().upper()
        if re.match("^\\d{6}\\.(SH|SZ|BJ)$", c) and c not in out:
            out.append(c)
    return out


def normalize_symbols(symbols: List[Any]) -> List[str]:
    """Non-A-share ticker symbols (NVDA.O / 0981.HK / BABA etc.)"""
    out = []
    for s in symbols:
        s = str(s).strip().upper()
        if re.match("^[A-Z0-9.\\-]{2,12}$", s) and s not in out:
            out.append(s)
    return out[:10]


def normalize_forecasts(
    forecasts: List[Any], ts_codes: List[str], market: str
) -> List[Dict[str, Any]]:
    out = []
    for f in forecasts:
        if not isinstance(f, dict):
            continue
        metric = str(f.get("metric") or "").strip()
        if metric not in _METRIC_WHITELIST:
            continue
        try:
            value = float(f.get("value"))
        except (TypeError, ValueError):
            continue
        symbol = str(f.get("symbol") or "").strip().upper()
        ts_code = None
        if re.match("^\\d{6}\\.(SH|SZ|BJ)$", symbol):
            ts_code, symbol = (symbol, None)
        elif not symbol:
            if ts_codes:
                ts_code = ts_codes[0]
            elif market != "A股":
                symbol = ""
            else:
                continue
        out.append(
            {
                "ts_code": ts_code,
                "symbol": symbol or None,
                "market": market,
                "metric": metric,
                "period": str(f.get("period") or "")[:16],
                "value": value,
                "unit": str(f.get("unit") or "")[:16],
                "confidence": float(f.get("confidence") or 80.0),
                "raw_text": str(f.get("raw_text") or "")[:500],
            }
        )
    return out


def prepare_extraction(row, data):
    """Normalize a model result into metadata, forecasts and the UI summary."""
    market = str(data.get("market") or "其他").strip()
    if market not in MARKETS:
        market = "其他"
    codes = normalize_codes(data.get("ts_codes") or [])
    symbols = normalize_symbols(data.get("symbols") or [])
    commodities = [str(c)[:32] for c in (data.get("commodities") or [])][:10]
    tags = data.get("tags") or {}
    rating = str(data.get("rating") or "无")
    if rating not in _RATING_WHITELIST:
        rating = "无"
    forecasts = normalize_forecasts(data.get("forecasts") or [], codes, market)
    fields = {
        "analysis_json": json.dumps(data, ensure_ascii=False),
        "market": market,
        "tags_industries": join_tags(tags.get("industries")),
        "tags_regions": join_tags(tags.get("regions")),
        "tags_commodities": join_tags(tags.get("commodities")) or ",".join(commodities),
        "tags_themes": join_tags(tags.get("themes")),
        "tags_targets": join_tags(tags.get("targets"))
        or ",".join(str(n)[:32] for n in (data.get("company_names") or [])[:5]),
    }
    if codes and not row.get("ts_code") and not row.get("symbols"):
        fields["ts_code"] = codes[0]
    if symbols and not row.get("symbols"):
        fields["symbols"] = ",".join(symbols)
    for key, length in (("report_type", 16), ("author", 64)):
        if data.get(key) and not row.get(key):
            fields[key] = str(data[key])[:length]
    published = str(data.get("publish_date") or "").strip()[:10]
    if (
        published
        and not row.get("publish_date")
        and re.match(r"^\d{4}-\d{2}-\d{2}$", published)
    ):
        fields["publish_date"] = published
    if data.get("org_name") and not row.get("org_name"):
        fields["org_name"] = normalize_org_name(str(data["org_name"]))[:64]
    title = display_title(
        (row.get("org_name") or "").strip() or normalize_org_name(data.get("org_name")),
        row.get("file_name") or row.get("title") or "",
    )
    if title and title != row.get("title"):
        fields["title"] = title[:200]
    summary = {
        "report_id": row["report_id"],
        "title": title or row.get("title"),
        "market": market,
        "rating": rating,
        "report_type": str(data.get("report_type") or "")[:16],
        "ts_code": codes[0] if codes else None,
        "symbols": symbols,
        "org_name": str(data.get("org_name") or row.get("org_name") or "")[:64],
        "core_view": str(data.get("core_view") or "")[:200],
        "key_points": [str(k)[:80] for k in (data.get("key_points") or [])[:6]],
        "forecast_count": len(forecasts),
        "forecasts": forecasts,
    }
    return fields, forecasts, summary
