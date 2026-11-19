"""Snapshot-pinned, score-filtered visual digest; no new trading decisions."""
import csv
from datetime import date
from html import escape
from io import StringIO
import math
from operator import ge, gt

from skills.trend.chart_history import stock_chart as stock_detail
from tools.market.chart_image import DIGEST_DAILY_BARS, MINUTE_BARS, render_digest_chart
from tools.market.trend_data import digest_stock_context
from tools.market.universe import risk_name

DEFAULT_EMAIL_MIN_SCORE = 75
TYPES = {"trend_pullback": ("↑ 趋势回调", "#80b8ff"), "base_rebound": ("■ 底部反转", "#c6a0f6")}
CHARTS = {
    "1D": ("日 K", f"最近 {DIGEST_DAILY_BARS} 个交易日详图 · 近 5 年价格与波动概览"),
    "60F": ("60F", f"最近 {MINUTE_BARS} 根 · 查看趋势结构和局部触发"),
}


def opportunity(row):
    context = row.get("context_pattern")
    if context in TYPES:
        return TYPES[context]
    kind = row.get("pattern") or row.get("family")
    if not kind and (row.get("setup_phase") == "pullback_resume" or row.get("startup", {}).get("phase") == "pullback_resume"):
        kind = "trend_pullback"
    if not kind and row.get("reversal_ignition"):
        kind = "base_rebound"
    return TYPES.get(kind, ("· 待识别", "#aaa294"))


def select_stocks(snapshot, minimum=60., observations=True, *, exclusive=False):
    cfg = snapshot["metadata"]["config"]
    meets_threshold = gt if exclusive else ge
    selected = []
    for row in snapshot["data"]:
        try:
            score = float(row.get("score", 0))
        except (ValueError, TypeError):
            continue
        if not math.isfinite(score) or not meets_threshold(score, minimum) or risk_name(row.get("name")):
            continue
        # Same candidate view as StockDashboard; mature/rejected structures do
        # not become digest candidates merely because a stale score is high.
        watch = (bool(row.get("setup_watch")) if cfg.get("risk_setups") else
                 bool(row.get("trend60", {}).get("fresh")) if cfg.get("fresh_reversal") else
                 bool(row.get("eligible")))
        if row.get("eligible") or observations and watch:
            selected.append(row)
    return sorted(selected, key=lambda r: (not bool(r.get("eligible")), -float(r["score"]), r["ts_code"]))


def stock_cards(snapshot, rows):
    if not rows:
        return []
    timeframes = snapshot["metadata"].get("timeframes", {})
    daily_day = str(timeframes.get("1D", {}).get("as_of", snapshot["as_of"]))[:10]
    context = digest_stock_context([row["ts_code"] for row in rows], daily_day)
    run_key = str(snapshot["run_id"]).replace("-", "")[:12]
    cards = []
    for row in rows:
        code = row["ts_code"]
        charts = {}
        for period in CHARTS:
            cutoff = timeframes.get(period, {}).get("as_of", snapshot["as_of"])
            detail = stock_detail(code, snapshot["metadata"]["start"], cutoff,
                                  snapshot["metadata"]["config"], period)
            if detail.get("issue") or not detail["data"] or str(detail["data"][-1]["bar_end"])[:10] != str(cutoff)[:10]:
                raise ValueError(f"{row['ts_code']} {period}图表数据不完整，日报未发送")
            if detail.get("price_basis") != "forward_asof":
                raise ValueError("日报图表必须使用与看板相同的真实因子前复权")
            cid = f"stock-{code.replace('.', '-')}-{period.lower()}.{run_key}@afr.invalid"
            charts[period] = {"cid": cid, "png": render_digest_chart(detail, period, stop=_number(row.get("stop_price")))}
        cards.append({"row": row, "charts": charts, "notes": stock_notes(row, context.get(code, {}))})
    return cards


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


def stock_notes(row, context):
    industry = context.get("industry_l2")
    if industry:
        basics = [f"申万二级行业 {industry}"]
    elif row.get("industry"):
        basics = [f"行业 {row['industry']}"]
    else:
        basics = []
    market_cap = _number(context.get("total_mv"))
    pe = _number(context.get("pe_ttm"))
    pb = _number(context.get("pb"))
    if market_cap is not None and market_cap > 0:
        basics.append(f"总市值 {market_cap / 10000:,.1f} 亿元")
    if pe is not None and pe > 0:
        basics.append(f"市盈率 TTM {pe:.1f} 倍")
    if pb is not None and pb > 0:
        basics.append(f"市净率 {pb:.2f} 倍")

    trend = [row["parent_trend_label"]] if row.get("parent_trend_label") else []
    pullback = _number(row.get("parent_pullback_bars"))
    if pullback is not None and pullback > 0:
        trend.append(f"母趋势已回调 {pullback:g} 个交易日")
    close = _number(row.get("metrics", {}).get("close"))
    if close is not None:
        trend.append(f"收盘 {close:.2f} 元")
    pct = _number(context.get("pct_chg"))
    if pct is not None:
        move = f"收涨 {pct:.2f}%" if pct > 0 else f"收跌 {-pct:.2f}%" if pct < 0 else "平收"
        trend.append(f"当日{move}")
    volume_ratio = _number(row.get("metrics", {}).get("volume_ratio"))
    if volume_ratio is not None:
        description = "成交量为前 20 日均量的" if row.get("timeframe") == "1D" else "信号周期量比"
        trend.append(f"{description} {volume_ratio:.2f} 倍")
    state = (row.get("trend60") or {}).get("state")
    if state:
        trend.append(f"60F {state}")

    reason = row.get("reason")
    if row.get("eligible"):
        signal = reason.rstrip("。") + "。" if reason else "已满足当前入场规则。"
    elif reason:
        pending = reason.rstrip("。").replace(" / ", "、")
        signal = f"继续观察，尚待确认 {pending}。"
    else:
        signal = "继续观察，等待日线确认。"
    risk = []
    for key, label, unit in (("stop_price", "参考止损", " 元"), ("risk_pct", "风险距离", "%")):
        value = _number(row.get(key))
        if value is not None and value > 0:
            risk.append(f"{label} {value:.2f}{unit}")
    return {
        "标的概况": "，".join(basics) + "。" if basics else "同日标的资料暂缺。",
        "走势解读": "，".join(trend) + "。" if trend else "关注下方日 K 和 60F 结构。",
        "信号与风险": " ".join([signal, "，".join(risk) + "。" if risk else ""]).strip(),
    }


def _candidate_cards(cards):
    groups = []
    for eligible, anchor, title in ((True, "entries", "入场候选"), (False, "observations", "观察候选")):
        members = [(number, card) for number, card in enumerate(cards, 1)
                   if bool(card["row"].get("eligible")) == eligible]
        if not members:
            continue
        groups.append(f'<a id="{anchor}" name="{anchor}"></a><h2 style="font-size:21px;margin:28px 0 8px;color:#172b40">{title} · {len(members)}只</h2>')
        for number, card in members:
            row = card["row"]
            label, _ = opportunity(row)
            notes = "".join(
                f'<p style="margin:5px 0;color:#25364a;font-size:15px;line-height:1.65">'
                f'<strong>{escape(heading)}</strong>　{escape(text)}</p>'
                for heading, text in card["notes"].items()
            )
            links = "　".join(
                f'<a href="#chart-{number}-{period.lower()}" style="color:#185694;text-decoration:underline">{title}图</a>'
                for period, (title, _) in CHARTS.items()
            )
            identity = " · ".join(escape(str(value)) for value in (row["ts_code"], row.get("board")) if value)
            groups.append(f'''<a id="brief-{number}" name="brief-{number}"></a><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="border-top:1px solid #dce4ec;background:#ffffff"><tr><td style="padding:14px 0 15px">
<h3 style="font-size:18px;color:#152c43;margin:0 0 4px">{number:02d}　{escape(row["name"])}</h3>
<p style="font-size:13px;color:#4b5d71;margin:0 0 8px">{identity}　<strong style="color:#234d76">{float(row["score"]):.1f} 分</strong>　{escape(label)}</p>
{notes}<p style="margin:8px 0 0;font-size:13px">{links}</p>
{_stock_figures(card, number)}
</td></tr></table>''')
    return "".join(groups)


def _stock_figures(card, number):
    name = escape(card["row"]["name"])
    sections = []
    for period, (title, caption) in CHARTS.items():
        cid = escape(card["charts"][period]["cid"], quote=True)
        anchor = f"chart-{number}-{period.lower()}"
        sections.append(f'''<a id="{anchor}" name="{anchor}"></a><p style="margin:12px 0 7px;color:#4b5d71;font-size:13px">
<strong style="color:#172b40">{title}</strong>　{caption}</p>
<img src="cid:{cid}" width="936" alt="{name} {title}前复权K线" style="display:block;width:100%;max-width:100%;height:auto;border:0">''')
    return "".join(sections)


def render_html(snapshot, cards, report_day: date, minimum=DEFAULT_EMAIL_MIN_SCORE, *, preview=False, delayed=False, test=False):
    cutoff = str(snapshot["as_of"]).replace("T", " ")[:16] if snapshot else "暂无"
    entries = sum(bool(card["row"].get("eligible")) for card in cards)
    observations = len(cards) - entries
    links = []
    for number, anchor, label in ((entries, "entries", "入场候选"), (observations, "observations", "观察候选")):
        if number:
            links.append(f'<a href="#{anchor}" style="color:#185694;text-decoration:underline">{label} {number} 只</a>')
    navigation = "　·　".join(links)
    alert = ("测试邮件" if test else "本地预览" if preview else "行情更新延迟" if delayed else
             "休市日报" if snapshot and cutoff[:10] != report_day.isoformat() else "盘后日报")
    if delayed:
        summary = "当天行情与快照尚未齐全。正式日报将在数据更新后发送。"
    elif cards:
        summary = navigation
    else:
        summary = "本期没有符合评分门槛的候选。"
    patterns = {}
    for card in cards:
        label, _ = opportunity(card["row"])
        name = label.split(maxsplit=1)[-1]
        patterns[name] = patterns.get(name, 0) + 1
    overview = "，".join(f"{escape(name)} {count} 只" for name, count in patterns.items())
    overview_html = f'<p style="margin:6px 0 0;color:#365c80;font-size:13px">{overview}</p>' if overview else ""
    legend = '<p style="margin:6px 0 0;color:#4b5d71;font-size:12px">前复权 · M8 / M13 / M21　▲ 日 K 入场标记　● 60F 观察标记</p>' if cards else ""
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Alpha Finance Radar · {report_day}</title></head>
<body style="margin:0;padding:0;background:#edf2f7;color:#25364a;font-family:'Microsoft YaHei',Arial,sans-serif">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#edf2f7" style="table-layout:fixed"><tr><td align="center" style="padding:18px 8px">
<!--[if mso]><table role="presentation" width="960" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#ffffff" style="width:100%;max-width:960px;table-layout:fixed">
<tr><td style="padding:22px 12px 14px;border-top:5px solid #1c5680">
<p style="color:#365c80;font-size:12px;margin:0 0 8px">Alpha Finance Radar · {alert} · {report_day}</p>
<h1 style="font-size:28px;color:#142a40;margin:0 0 8px">盘后选股简报</h1>
<p style="margin:0 0 10px;color:#4b5d71;font-size:13px;line-height:1.6">数据截至 {escape(cutoff)} · 评分 &gt; {minimum:g}</p>
<p style="margin:0;font-size:15px;line-height:1.75">{summary}</p>
{overview_html}{legend}
</td></tr>
<tr><td style="padding:0 12px 24px">{_candidate_cards(cards)}</td></tr>
</table><!--[if mso]></td></tr></table><![endif]--></td></tr></table></body></html>'''


def export_csv(rows):
    output = StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["代码", "名称", "板块", "机会类型", "评分", "状态", "母趋势", "参考止损", "风险距离%", "筛选解释"])
    for r in rows:
        values = [r["ts_code"], r["name"], r.get("board"), opportunity(r)[0], r["score"],
                  "入场候选" if r.get("eligible") else "观察候选", r.get("parent_trend_label"),
                  r.get("stop_price"), r.get("risk_pct"), r.get("reason")]
        writer.writerow(["'"+v if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in values])
    return output.getvalue()
