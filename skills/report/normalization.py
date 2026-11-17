"""Pure report metadata normalization, shared by streaming and batch extraction."""

import re
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
