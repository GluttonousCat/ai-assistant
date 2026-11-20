"""Extract and verify evidence per batch, then assemble one report analysis."""

from decimal import Decimal
import json
import re

from core.json_response import parse_json_response
from core.logger import get_logger
from core.offpeak import OffPeakEnded
from skills.report.document import extraction_batches
from skills.report.forecast_evidence import bind_forecast
from skills.report.prompts import REPORT_EXTRACT_PROMPT, REPORT_LOCATE_PROMPT, REPORT_REDUCE_PROMPT

logger = get_logger(__name__)

ANALYSIS_VERSION = 4
_EVIDENCE_KINDS = {"driver", "catalyst", "valuation", "revision", "risk", "industry"}
_INSIGHT_TYPES = ("theses", "assumptions", "changes", "watchpoints")
_NUMBER = re.compile(r"\(-?\d+(?:\.\d+)?\)|[+-]?(?:\d+(?:\.\d+)?|\.\d+)")
_OVERVIEW_FIELDS = (
    "market", "ts_codes", "symbols", "company_names", "commodities", "report_type",
    "org_name", "author", "publish_date", "tags",
)
SPEAKERS = {
    "broker": "券商", "management": "公司管理层", "survey": "调查受访者",
    "third_party": "第三方", "unknown": "归属待核实",
}
STANCES = {"observation": "观察", "expectation": "预期", "conditional": "条件判断", "risk": "风险", "unknown": "语气待核实"}

# A literal quotation can still be paired with a paraphrase of the opposite
# meaning. Reject clear loss of a qualifier; the original remains authoritative.
_QUALIFIERS = tuple(re.compile(pattern, re.I) for pattern in (
    r"\b(?:not|never|cannot|unlikely|unproven|unverified|no longer|no (?:evidence|plans?|increase)|falls? short)\b|n['’]t\b|不(?:会|能|再|足|太可能|认为|预计|预期|排除|及预期|看好|乐观|支持|增长|上升|下跌)|尚未|并未|并不|没有|未能|未[经获]?(?:证实|验证)|难以",
    r"\b(?:if|unless|provided that|conditional on|contingent on|assuming|scenarios?|(?:base|bull|bear) case)\b|如果|若|一旦|除非|假设|取决于|前提|条件下|情况下|情[景境]|时(?:才|预计|可能|将|会)",
    r"\b(?:may|might|could|possibly|potential(?:ly)?|uncertain(?:ty)?|likely|unlikely)\b|可能|有望|或将|或会|或可|倾向|不确定|风险|难以",
))
_MANAGEMENT = re.compile(
    r"\b(?:management|(?:the )?company|the CEO|the CFO)\s+(?:(?:is|was|remains?|has|had|does not|did not)\s+)?"
    r"(?:(?:more|less|still|increasingly|cautiously)\s+)?"
    r"(?:expect|project|anticipat|guid|said|says|believe|indicat|target|confiden|optimistic|cautious|sees)"
    r"|(?:管理层|公司|首席执行官|董事长)(?:表示|预计|预期|指引|认为|计划|称|更?有信心)", re.I
)
_SURVEY = re.compile(r"\b(?:respondents?|surveyed (?:investors|fund managers)|net % expecting|net % saying)\b|受访(?:者|基金经理|投资者)|调查对象", re.I)
_CONSENSUS = re.compile(r"\b(?:consensus (?:estimates?|expects?|forecasts?)|according to consensus)\b|一致预期(?:为|显示|预计)", re.I)
_AUTHOR_VOICE = re.compile(r"\bwe\s+(?:(?:do not|don't|still|now)\s+)?(?:expect|believe|disagree|forecast|estimate|think|see)\b|我们(?:不|仍|现在)?(?:预计|认为|认同|看好|预期)", re.I)
_UP = re.compile(r"\b(?:ris(?:e|es|ing)|rose|rais\w*|increas\w*|upgrad\w*)\b|上升|上调|增长|提升|提高|上修", re.I)
_DOWN = re.compile(r"\b(?:fall(?:s|ing)?|fell|declin\w*|reduc\w*|lower(?:ed)?|downgrad\w*)\b|下降|下调|下滑|下跌|减少|降低|下修", re.I)
_EXPECTED = re.compile(r"\b(?:expect(?:s|ed)?|estimat(?:e|es|ed)|forecast(?:s|ed)?|project(?:s|ed)?)\b|预计|估计|推测|预测|预期|或将|认为", re.I)
_RATINGS = {
    "买入": "buy", "增持": "overweight|accumulate", "推荐": "recommend",
    "持有": "hold", "中性": "neutral|equal.weight|market.perform",
    "卖出": "sell", "跑赢大盘": "outperform",
    "跑输大盘": "underperform|underweight",
}


_RATING_EXCLUSION = re.compile(
    r"\b(?:not|no longer|previous(?:ly)?|prior|historical|formerly|historically|hypothetical|would|"
    r"until|consensus|definitions?|distribution|glossary|rating system|means|denotes|is defined as|"
    r"used to|was rated|had rated)\b"
    r"|此前|曾经|曾给予|曾给出|历史评级|过往|原评级|原先|上次|前次评级|评级(?:标准|体系|分布|说明|定义|释义|记录)"
    r"|意味着|定义为|是指|不(?:再|予|作|给|维持|是)|并非|未(?:予|给|对|评级)|若|假设", re.I
)


def _rating_matches(rating, quote, neighboring=""):
    if rating not in _RATINGS or any(marker.search(quote) or marker.search(neighboring)
                                     for marker in (_MANAGEMENT, _SURVEY, _CONSENSUS)):
        return False
    if _RATING_EXCLUSION.search(quote) or _RATING_EXCLUSION.search(neighboring):
        return False
    found = {label for label, aliases in _RATINGS.items()
             if label in quote or re.search(r"\b(?:" + aliases + r")\b", quote, re.I)}
    if found != {rating}:
        return False
    value = r"(?:" + rating + r"|\b(?:" + _RATINGS[rating] + r")\b)"
    gap = r"[\s:：|\-–“”「」‘’\"']*"
    # A vocabulary match (“buy diesel engines”) is insufficient. Require a
    # current rating field, or the analyst's affirmative rating action.
    field = r"(?:\b(?:stock |investment |our |current )?rating\b|(?:投资|股票|公司|当前)?评级)(?:为|是|\s+is)?" + gap + value
    action = (r"(?:\bwe\s+(?:maintain|reiterate|rate|initiate|assign)\b|维持|重申|给予|首次覆盖)"
              r"[^。！？.!?\n]{0,48}?" + value + gap + r"(?:评级|\brating\b)")
    return bool(re.search(field, quote, re.I) or re.search(action, quote, re.I))


def _framing(item, quote, material):
    attribution = item.get("attribution")
    if not isinstance(attribution, str) or attribution not in SPEAKERS:
        attribution = "unknown"
    # “Unlike consensus estimates, we expect …” is the author's view.
    author_contrasts = (material == "original" and attribution == "broker" and _AUTHOR_VOICE.search(quote)
                        and re.search(r"unlike|disagree|whereas|\bbut we\b|不同意|有别于|但我们", quote, re.I))
    if not author_contrasts:
        if _MANAGEMENT.search(quote):
            attribution = "management"
        elif _SURVEY.search(quote):
            attribution = "survey"
        elif _CONSENSUS.search(quote):
            attribution = "third_party"
        elif attribution == "broker" and material != "original":
            attribution = "unknown"
    stance = item.get("stance")
    if not isinstance(stance, str) or stance not in STANCES:
        stance = "unknown"
    if _QUALIFIERS[1].search(quote):
        stance = "conditional"
    elif item.get("kind") == "risk":
        stance = "risk"
    elif stance == "observation" and _QUALIFIERS[2].search(quote):
        stance = "expectation"
    return {"attribution": attribution, "stance": stance}


class InvalidExtraction(ValueError):
    """The model did not return the report object required to finish extraction."""


def saved_analysis_version(raw):
    """Unversioned or malformed historical output predates versioned extraction."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return 0
    version = raw.get("analysis_version") if isinstance(raw, dict) else None
    if type(version) is int:
        return version
    if isinstance(version, str) and version.isascii() and version.isdigit() and len(version) <= 9:
        return int(version)
    return 0


def clean_text(value, limit):
    return value.strip()[:limit] if isinstance(value, str) else ""


def list_items(value, limit=100):
    return value[:limit] if isinstance(value, list) else []


def text_items(value, limit, chars):
    return list(dict.fromkeys(
        text for item in list_items(value) if (text := clean_text(item, chars))
    ))[:limit]


def _compact(text):
    return re.sub(r"\s+", "", text).casefold()


def _locations(items, segments):
    source_text = {s.number: s for s in segments}
    for item in list_items(items):
        if not isinstance(item, dict) or not isinstance(item.get("source"), dict):
            continue
        source = item["source"]
        number = source.get("segment")
        quote = clean_text(source.get("quote"), 361)
        if type(number) is not int or number not in source_text or not 8 <= len(quote) <= 360:
            continue
        segment = source_text[number]
        if not any(c.isalnum() for c in quote):
            continue
        if quote not in segment.text:
            # The model may flatten line breaks in tables. Store the actual matched
            # text so the citation still reproduces the page's case and spacing.
            pattern = r"\s*".join(re.escape(char) for char in quote if not char.isspace())
            match = re.search(pattern, segment.text, flags=re.IGNORECASE)
            if not match:
                continue
            quote = match.group()
        yield item, {"segment": number, "page": segment.page, "quote": quote}


def _numbers(text):
    plain = re.sub(r"(?<=\d)[,，](?=\d{3}(?:\D|$))", "", text.replace("−", "-"))
    for match in _NUMBER.findall(plain):
        token = "-" + match[1:-1] if match.startswith("(") and match[1] != "-" else match.strip("()")
        yield Decimal(token)


_AMOUNT = r"([+-]?\d[\d,，]*(?:\.\d+)?)"
_UNIT = r"(?:\s*(?:%|％|个百分点|percentage points?|pp\b|bps?\b|basis points?|万亿|十亿|百万|千万|万|亿|billion|million|bn\b|mn\b))?"
_FROM_TO = re.compile(r"(?:\bfrom\s*|[从由])" + _AMOUNT + _UNIT + r"\s*(?:to|至|到|升至|降至|降到|升到|上调至|下调至)\s*" + _AMOUNT, re.I)
_TO_FROM = re.compile(r"(?:\bto\s*)" + _AMOUNT + _UNIT + r"\s*,?\s*(?:(?:up|down)\s+)?from\s*" + _AMOUNT, re.I)
_SCALE = re.compile(r"\s*(万亿|十亿|百万|千万|万|亿|trillion|billion|million|thousand|bn\b|mn\b|[bmk]\b)", re.I)
_SCALES = {"万亿": 12, "trillion": 12, "十亿": 9, "billion": 9, "bn": 9, "b": 9,
           "亿": 8, "千万": 7, "百万": 6, "million": 6, "mn": 6, "m": 6, "万": 4, "thousand": 3, "k": 3}
_PERCENT = re.compile(r"\s*(percentage\s+points?|percent(?:age)?\b|个百分点|%|％|百分点|pp\b|ppts?\b|basis\s+points?|bps?\b|基点)", re.I)
_CURRENCIES = {"usd": "USD", "us$": "USD", "美元": "USD", "rmb": "CNY", "cny": "CNY",
               "人民币": "CNY", "hkd": "HKD", "hk$": "HKD", "港元": "HKD", "港币": "HKD",
               "eur": "EUR", "欧元": "EUR", "jpy": "JPY", "日元": "JPY"}
_CURRENCY = "|".join(re.escape(k) for k in _CURRENCIES)


def _transitions(text):
    moves = set()
    for pattern, reversed_order in ((_FROM_TO, False), (_TO_FROM, True)):
        for match in pattern.finditer(text):
            pair = tuple(Decimal(x.replace(",", "").replace("，", "")) for x in match.groups())
            moves.add(pair[::-1] if reversed_order else pair)
    return moves


def _units(text):
    """Units attached to actual numbers, keeping %/pp/bps, currency and scale separate."""
    units = set()
    for match in re.finditer(_AMOUNT, text):
        value = Decimal(match[1].replace(",", "").replace("，", ""))
        after = text[match.end():]
        pct = _PERCENT.match(after)
        if pct:
            token = pct[1].lower()
            kind = "bps" if re.fullmatch(r"bps?|basis\s+points?|基点", token) else (
                "%" if token in ("%", "％", "percent", "percentage") else "pp")
            units.add((value, "rate_unit", kind))
        scale = _SCALE.match(after)
        if scale:
            units.add((value, "scale", _SCALES[scale[1].lower()]))
        before = text[max(0, match.start() - 8):match.start()]
        currency = re.search(r"(" + _CURRENCY + r")\s*$", before, re.I)
        if not currency:
            currency = re.match(r"\s*(" + _CURRENCY + ")", after[scale.end() if scale else 0:], re.I)
        if currency:
            units.add((value, "currency", _CURRENCIES[currency[1].lower()]))
    return units


def _quantities_match(statement, quote):
    values = set(_numbers(statement))
    if not values.issubset(_numbers(quote)):
        return False
    moves = _transitions(quote)
    if any((end, start) in moves and (start, end) not in moves for start, end in _transitions(statement)):
        return False
    source_units = _units(quote)
    statement_units = _units(statement)
    for value, family, unit in statement_units:
        known = {u for v, f, u in source_units if v == value and f == family}
        if unit not in known:
            return False
    for value, family, _ in source_units:
        if value in values and not any(v == value and f == family for v, f, _ in statement_units):
            return False
    return True


def _claim_source(source, segment):
    # Restore a speaker or qualifier clipped from the surrounding sentence.
    # A physical PDF line can wrap mid-clause; a paragraph or table cell ends it.
    quote = source["quote"]
    start = segment.text.find(quote)
    if start < 0:
        return source
    edge = r"(?:[。！？!?|]|\.\s|\n\s*\n)"
    before = "\n".join(re.split(edge, segment.text[:start])[-1].split("\n")[-2:])
    after = "\n".join(re.split(edge, segment.text[start + len(quote):], maxsplit=1)[0].split("\n")[:2])
    if re.search(r"[。！？!?\n.]$", quote):
        after = ""
    whole = (before + quote + after).strip()
    if whole == quote:
        return source
    markers = (*_QUALIFIERS, _MANAGEMENT, _SURVEY, _CONSENSUS, _AUTHOR_VOICE)
    if any(marker.search(whole) and not marker.search(quote) for marker in markers):
        return {**source, "quote": whole} if len(whole) <= 360 else None
    return source


def _grounded_claim(item, source, material, segment):
    source = _claim_source(source, segment)
    if source is None:
        return None
    statement = item.get("statement")
    if not isinstance(statement, str) or not 0 < len(statement.strip()) <= 300:
        return None
    statement = statement.strip()
    quote = source["quote"]
    if not set(_numbers(statement)).issubset(_numbers(quote)):
        return None
    # Chinese excerpts can be shown verbatim, keeping every condition and
    # speaker. English/multilingual excerpts need a Chinese reference rendering.
    cjk = len(re.findall(r"[\u3400-\u9fff]", quote))
    if cjk >= 4 and cjk * 2 >= len(re.findall(r"[a-z]", quote, re.I)):
        statement = quote
    elif not re.search(r"[\u3400-\u9fff]", statement):
        return None
    elif not _quantities_match(statement, quote):
        return None
    elif any(marker.search(quote) and not marker.search(statement) for marker in _QUALIFIERS):
        return None
    elif any(marker.search(statement) and not marker.search(quote) for marker in _QUALIFIERS[:2]):
        # Added negation or an invented condition can reverse a valid original.
        return None
    elif _EXPECTED.search(quote) and not (_EXPECTED.search(statement) or _QUALIFIERS[2].search(statement)):
        return None
    elif any(a.search(quote) and not b.search(quote) and b.search(statement) and not a.search(statement)
             for a, b in ((_UP, _DOWN), (_DOWN, _UP))):
        return None
    framing = _framing(item, quote, material)
    if framing["attribution"] in ("management", "survey") and re.search(
        r"券商(?:预计|预计将|认为|判断|预测|看好)|分析师(?:预计|认为|判断)", statement
    ) and statement != quote:
        return None
    return {"statement": statement, **framing, "source": source}


def locate_excerpt(item, segments):
    """Recover only the original text and its actual physical location."""
    for segment in segments:
        for _, source in _locations([item], [segment]):
            located = _claim_source(source, segment)
            if located:
                return located
    return None


def verify_claim(item, segments, *, material="unknown"):
    """Locate one cited claim in the supplied passages, then check its wording."""
    for segment in segments:
        for _, source in _locations([item], [segment]):
            claim = _grounded_claim(item, source, material, segment)
            if claim:
                return claim
    return None


def _verified_insight(item, statement, source, quoted_numbers):
    fields = {
        key: value
        for key in ("subject", "indicator", "condition", "horizon")
        if (value := clean_text(item.get(key), 120))
    }
    if item.get("insight_type") == "watchpoints" and not (fields.get("indicator") or fields.get("condition")):
        return None
    # A real quote alone does not support an invented price threshold or date.
    if any(n not in quoted_numbers for value in fields.values() for n in _numbers(value)):
        return None
    if any(_compact(value) not in _compact(statement) and _compact(value) not in _compact(source["quote"])
           for value in fields.values()):
        return None
    return {"statement": statement, **fields, "source": source}


def _verified_notes(claims, proposals, batch, material):
    """Bind only known claim IDs; replacement model output may only supply a citation."""
    verified = {}
    by_number = {segment.number: segment for segment in batch}
    for item, source in _locations(proposals, batch):
        index = item.get("id")
        if type(index) is int and index in claims:
            note = _grounded_claim(claims[index], source, material, by_number[source["segment"]])
            if note:
                verified.setdefault(index, note)
    return verified


def _summary(evidence, material):
    core = next((note for note in evidence if material == "original"
                 and note["attribution"] == "broker" and note.get("insight_type") == "theses"), None)
    points = [note for note in evidence if note is not core and note["kind"] != "risk"][:6]
    risks = [note for note in evidence if note["kind"] == "risk"][:10]

    def labeled(note):
        speaker = SPEAKERS[note["attribution"]]
        return f"{'材料转述·' if material == 'commentary' else ''}{speaker}：{note['statement']}"

    return {
        "core_view": labeled(core) if core else "",
        "key_points": [labeled(note) for note in points],
        "risks": [labeled(note) for note in risks],
        "summary_evidence": {"core_view": core, "key_points": points, "risks": risks},
    }


def _relocate_notes(llm, claims, batch, body, material):
    payload = [
        {"id": index, **{key: clean_text(item.get(key), 300) for key in
                         ("statement", "attribution", "stance", "subject", "condition", "indicator", "horizon") if item.get(key)}}
        for index, item in claims.items()
    ]
    try:
        raw = llm.invoke(REPORT_LOCATE_PROMPT.format(claims=json.dumps(payload, ensure_ascii=False), report_text=body))
        data = parse_json_response(raw)
        return _verified_notes(claims, data.get("sources"), batch, material) if isinstance(data, dict) else {}
    except OffPeakEnded:
        raise
    except Exception as exc:
        logger.warning("研报出处复核调用失败，保留已核对结果: %s", type(exc).__name__)
        return {}


def _overview(data):
    out = {key: clean_text(data.get(key), 200) for key in _OVERVIEW_FIELDS}
    for key in ("ts_codes", "symbols", "company_names", "commodities"):
        out[key] = text_items(data.get(key), 10, 150)
    tags = data.get("tags")
    out["tags"] = {
        key: text_items(tags.get(key), 12, 64)
        for key in ("targets", "industries", "regions", "commodities", "themes")
    } if isinstance(tags, dict) else {}
    return out


def _parse_object(raw):
    data = parse_json_response(raw)
    if not isinstance(data, dict) or not any(key in data for key in ("core_view", "key_points", "market", "evidence", "forecasts")):
        raise InvalidExtraction("模型输出缺少有效的研报分析 JSON")
    return data


def analyze_document(document, title, llm, *, streaming=False):
    """Yield progress and one verified analysis; unchecked model text stays internal."""
    batches = extraction_batches(document)
    if not batches:
        raise InvalidExtraction("研报没有可分析的正文")
    # The feed's short, signed commentary covers differ from the distribution
    # watermark on broker originals. Match only an actual first page.
    first = document.segments[0]
    signed_cover = first.page == 1 and len(first.text) < 500 and bool(
        re.search(r"(?:^|\n)公众号[：:]\s*猫哥读研报\s*$", first.text)
    )
    material = "commentary" if signed_cover else "unknown"
    material_source = ({"segment": first.number, "page": 1, "quote": first.text.strip()[-360:]}
                       if signed_cover else None)
    parts, forecasts, evidence = [], [], []
    forecast_excerpts = []
    ratings = {}
    insights = {name: [] for name in _INSIGHT_TYPES}
    seen = set()
    seen_insights = set()
    rejected = rejected_evidence = rejected_insights = 0
    repair_attempts = recovered = 0
    for index, batch in enumerate(batches, 1):
        yield {"type": "stage", "stage": "extract", "message": f"提取正文 {index}/{len(batches)} · 观点、预测与出处"}
        body = "\n\n".join(segment.render() for segment in batch)
        prompt = REPORT_EXTRACT_PROMPT.format(title=title, report_text=body)
        raw = "".join(llm.stream(prompt)) if streaming and len(batches) == 1 else llm.invoke(prompt)
        data = _parse_object(raw)
        if index == 1 and material == "unknown":
            for item, source in _locations([data.get("material")], batch):
                kind = item.get("type")
                if kind in ("original", "commentary") and source["segment"] == first.number and first.page in (None, 1):
                    material, material_source = kind, source

        rating = clean_text(data.get("rating"), 12)
        if material == "original" and rating and rating != "无":
            texts = {s.number: s.text for s in batch}
            for _, source in _locations([{"source": data.get("rating_source")}], batch):
                segment_text = texts[source["segment"]]
                start = segment_text.find(source["quote"])
                # Protect against “previous Stock Rating: Buy” cropped to just
                # the apparent rating field, including split PDF lines.
                left = re.split(r"[。！？.!?;；]|\n\s*\n", segment_text[max(0, start - 120):start])[-1]
                right = segment_text[start + len(source["quote"]):].split("\n", 1)[0][:64]
                if _rating_matches(rating, source["quote"], left + " " + re.split(r"[。！？.!?;；]", right)[0]):
                    ratings.setdefault(rating, source)
        segments = {s.number: s for s in batch}
        verified_forecasts = []
        for item, source in _locations(data.get("forecasts"), batch):
            binding = bind_forecast(item, source, segments[source["segment"]])
            if not binding:
                if source not in forecast_excerpts:
                    forecast_excerpts.append(source)
                continue
            forecast = {key: item.get(key) for key in ("symbol", "metric", "period", "value", "unit")}
            verified_forecasts.append({**forecast, **_framing(item, binding["source"]["quote"], material), **binding})
        rejected += len(list_items(data.get("forecasts"))) - len(verified_forecasts)
        forecasts.extend(verified_forecasts)
        notes = list_items(data.get("evidence"))
        claims = {i: item for i, item in enumerate(notes, 1) if isinstance(item, dict)
                  and clean_text(item.get("kind"), 24) in _EVIDENCE_KINDS and clean_text(item.get("statement"), 300)}
        proposals = [{"id": i, "source": item.get("source")} for i, item in claims.items()]
        verified = _verified_notes(claims, proposals, batch, material)
        if not repair_attempts and len(claims) >= 4 and 2 * len(verified) < len(claims):
            repair_attempts += 1
            yield {"type": "stage", "stage": "parse", "message": "复核未定位的证据出处"}
            missing = {i: item for i, item in claims.items() if i not in verified}
            repaired = _relocate_notes(llm, dict(list(missing.items())[:10]), batch, body, material)
            recovered += len(repaired)
            verified.update(repaired)
        rejected_evidence += len(notes) - len(verified)
        for i, claim in verified.items():
            item = claims[i]
            kind = clean_text(item.get("kind"), 24)
            source = claim["source"]
            statement = claim["statement"]
            key = kind, claim["attribution"], claim["stance"], _compact(statement)
            quoted_numbers = set(_numbers(source["quote"]))
            role = clean_text(item.get("insight_type"), 24)
            if role in insights:
                insight = _verified_insight(item, statement, source, quoted_numbers)
                if insight is not None:
                    insight.update(attribution=claim["attribution"], stance=claim["stance"])
                    insight_key = (role, tuple((k, _compact(v)) for k, v in insight.items() if k != "source"))
                    if insight_key not in seen_insights:
                        seen_insights.add(insight_key)
                        insights[role].append(insight)
                else:
                    rejected_insights += 1
            if key in seen:
                continue
            seen.add(key)
            evidence.append({"kind": kind, "insight_type": role if role in insights else "", **claim})
        parts.append(_overview(data))

    if len(parts) == 1:
        combined = {key: parts[0][key] for key in _OVERVIEW_FIELDS}
    else:
        yield {"type": "stage", "stage": "synthesize", "message": "合并分段元数据，按出处归集观点"}
        prompt = REPORT_REDUCE_PROMPT.format(title=title, parts=json.dumps(parts, ensure_ascii=False))
        raw = "".join(llm.stream(prompt)) if streaming else llm.invoke(prompt)
        combined = _overview(_parse_object(raw))

    analyzed = [segment for batch in batches for segment in batch]
    missing = sorted(set(document.missing_pages) | (
        {segment.page for segment in document.segments if segment.page is not None}
        - {segment.page for segment in analyzed}
    ))
    combined.update(
        **_summary(evidence, material),
        rating=next(iter(ratings)) if len(ratings) == 1 else "无",
        rating_source=next(iter(ratings.values())) if len(ratings) == 1 else None,
        analysis_version=ANALYSIS_VERSION,
        material_type=material,
        material_source=material_source,
        evidence=evidence,
        research_insights=insights,
        forecasts=forecasts,
        forecast_excerpts=forecast_excerpts,
        coverage={
            "source": document.source,
            "segment_count": len(document.segments),
            "analyzed_segments": len(analyzed),
            "page_count": document.page_count,
            "missing_pages": missing,
            "partial": bool(missing or document.incomplete_text or len(analyzed) < len(document.segments)),
            "rejected_forecasts": rejected,
            "rejected_evidence": rejected_evidence,
            "rejected_insights": rejected_insights,
            "evidence_repair_attempts": repair_attempts,
            "recovered_evidence": recovered,
        },
    )
    yield {"type": "result", "data": combined}
