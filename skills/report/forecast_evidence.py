"""Bind a proposed forecast to its metric, currency, scale and forecast column."""

from decimal import Decimal, InvalidOperation
import re

_METRICS = {
    "eps": r"\bEPS\b|earnings per share|每股收益|每股盈利",
    "net_profit": r"net (?:profit|income|earnings)|净利润|净利(?!率)",
    "revenue": r"\brevenues?\b|\bsales\b|营业(?:总)?收入|营收|销售收入",
    "roe": r"\bROE\b|return on equity|净资产收益率",
    "gross_margin": r"gross (?:profit )?margin|毛利率",
    "target_price": r"(?:share )?price target|target price|目标价",
}
_YEAR = re.compile(
    r"(?<![\d.])(?:12/(?P<short>\d{2})|(?:FY\s*)?(?P<long>20\d{2})(?P<sub>Q[1-4]|H[12])?)"
    r"(?P<flag>[AEF])?(?![\d.])", re.I,
)
_NUMBER = re.compile(r"\(?[+−-]?(?:\d{1,3}(?:[,，]\d{3})+|\d+)(?:\.\d+)?\)?")
_FORECAST = re.compile(r"预计|预期|预测|\b(?:forecast|project|expect|estimate)\w*\b", re.I)
_NON_BASE = re.compile(r"\b(?:previous|prior|consensus|actual|bull case|bear case)\b|实际值|原预测|前次预测|一致预期|乐观情景|悲观情景", re.I)
_CURRENCIES = {
    "cny": r"人民币|\b(?:rmb|cny)(?![a-z])", "usd": r"美元|美金|US\$|\bUSD(?![a-z])",
    "hkd": r"港[元币]|HK\$|\bHKD(?![a-z])", "eur": r"欧元|€|\bEUR(?![a-z])",
    "jpy": r"日[圆元]|\bJPY(?![a-z])", "twd": r"新台币|NT\$|\bTWD(?![a-z])",
    "krw": r"韩元|\bKRW(?![a-z])", "gbp": r"英镑|£|\bGBP(?![a-z])",
}
_SCALE_WORDS = {
    "万亿": 12, "trillion": 12, "十亿": 9, "bn": 9, "billion": 9, "亿": 8,
    "千万": 7, "百万": 6, "mn": 6, "million": 6, "万": 4, "千": 3, "thousand": 3,
}
_SCALES = re.compile(r"万亿|十亿|千万|百万|亿|万|千|trillion|billion|million|thousand|(?<![a-z])(?:bn|mn)(?![a-z])", re.I)


def _unit(text):
    if re.search(r"百分点|percentage points?|basis points?|基点|\bbps?\b", text, re.I):
        return None
    if re.search(r"%|％|\bpercent(?:age)?\b", text, re.I):
        return ("%", 0)
    currencies = [key for key, pattern in _CURRENCIES.items() if re.search(pattern, text, re.I)]
    if not currencies and "元" in text:
        currencies = ["cny"]
    scales = {_SCALE_WORDS[m[0].lower()] for m in _SCALES.finditer(text)}
    if len(currencies) == 1 and len(scales) <= 1:
        return currencies[0], next(iter(scales), 0)
    return None


def _periods(line):
    columns = []
    for year in _YEAR.finditer(line):
        period = (year["long"] or "20" + year["short"]) + (year["sub"] or "").upper()
        columns.append((period, (year["flag"] or "").upper()))
    return columns


def _values(text):
    values = []
    for match in _NUMBER.finditer(text):
        token = match[0].replace(",", "").replace("，", "").replace("−", "-")
        negative = token.startswith("(") and token.endswith(")")
        number = Decimal(token.strip("()"))
        values.append(-number if negative else number)
    return values


def bind_forecast(item, source, segment):
    """Return a verified row with any supporting header, or leave the excerpt unassigned."""
    metric = item.get("metric")
    if not isinstance(metric, str) or metric not in _METRICS:
        return None
    try:
        value = Decimal(str(item.get("value")))
    except InvalidOperation:
        return None
    if not value.is_finite() or not isinstance(item.get("unit"), str):
        return None
    unit = _unit(item["unit"])
    quote = source["quote"]
    labels = list(re.finditer(_METRICS[metric], quote, re.I))
    if len(labels) != 1 or unit is None:
        return None
    # Read the entire physical row (the model may have quoted just its first
    # two values). Sorted PDFs sometimes contain another text column to the left.
    start = segment.text.find(quote) + labels[0].start()
    preceding = segment.text[:start]
    lines_before = preceding.split("\n")
    label_prefix = lines_before[-1]
    if _NON_BASE.search(quote) or _NON_BASE.search(label_prefix[-16:]):
        return None
    row = segment.text[start:].split("\n", 1)[0].strip()
    if not row or len(row) > 360:
        return None
    row_source = {**source, "quote": row}
    periods = _periods(quote)
    requested = str(item.get("period") or "").upper().removesuffix("E")
    # A single period in prose must be explicitly forward-looking. An unmarked
    # fiscal year in a historical series cannot become an estimate.
    if metric == "target_price" or len(periods) == 1:
        if metric != "target_price":
            period, flag = periods[0]
            if period != requested or flag == "A" or (not flag and not _FORECAST.search(quote)):
                return None
        measured = _YEAR.sub("", quote)
        if _unit(quote) == unit and _values(measured) == [value]:
            return {"source": source}
        if metric == "target_price" and _unit(row) == unit and _values(row) == [value]:
            return {"source": row_source}
        return None

    values = _values(row)
    for line in reversed(lines_before[:-1][-12:]):
        columns = _periods(line)
        if len(columns) < 2:
            continue
        first_year = _YEAR.search(line)
        separators = _YEAR.sub("", line[first_year.start():])
        if re.search(r"[^\s/|,，;:()年月年度]", separators):
            continue  # Years in intervening prose are not column headers.
        header = line.strip()
        if (len(header) > 360 or len(columns) != len(values) or len({p for p, _ in columns}) != len(columns)
                or (_unit(row) or _unit(header)) != unit):
            return None
        for (period, flag), column_value in zip(columns, values):
            if period == requested and flag in ("E", "F") and column_value == value:
                return {"source": row_source, "header_source": {**source, "quote": header}}
        return None
    return None
