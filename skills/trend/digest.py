"""Snapshot-pinned, score-filtered visual digest; no new trading decisions."""
import csv
from datetime import date
from html import escape
from io import StringIO
import math
from operator import ge, gt

from skills.trend import MIN_CANDIDATE_SCORE
from skills.trend.chart_history import stock_chart as stock_detail
from tools.market.chart_image import DIGEST_DAILY_BARS, MINUTE_BARS, render_digest_chart
from tools.market.trend_data import digest_stock_context
from tools.market.universe import risk_name

DEFAULT_EMAIL_MIN_SCORE = MIN_CANDIDATE_SCORE
TYPES = {"trend_pullback": ("↑ 趋势回调", "#80b8ff"), "base_rebound": ("■ 底部反转", "#c6a0f6")}
CHARTS = {
    "1D": ("日 K", f"最近 {DIGEST_DAILY_BARS} 日 · ▲ 入场标记"),
    "60F": ("60F", f"最近 {MINUTE_BARS} 根 · ● 观察标记"),
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


def select_stocks(snapshot, minimum=None, observations=True, *, exclusive=False):
    cfg = snapshot["metadata"]["config"]
    if minimum is None:
        minimum = snapshot["metadata"].get("tracking_min_score", 60.)
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


def _stock_figures(card, number):
    name = escape(card["row"]["name"])
    figures = []
    for period, (title, caption) in CHARTS.items():
        cid = escape(card["charts"][period]["cid"], quote=True)
        anchor = f"chart-{number}-{period.lower()}"
        figures.append(f'''<a id="{anchor}" name="{anchor}"></a>
<p style="margin:16px 0 6px;color:#52645f;font-size:14px;line-height:1.55"><strong style="color:#17392f">{title}</strong> · {caption}</p>
<img src="cid:{cid}" width="644" alt="{name} {title}前复权K线" style="display:block;width:100%;max-width:644px;height:auto;border:0">''')
    return "".join(figures)


def _stock_card(card, number):
    row = card["row"]
    label, _ = opportunity(row)
    identity = " · ".join(escape(str(value)) for value in (row["ts_code"], row.get("board"), label) if value)
    notes = card["notes"]
    signal = escape(notes.get("信号与风险", "")).replace("。 ", "。<br>")
    paragraphs = "".join(
        f'<p style="margin:8px 0 0;font-size:16px;line-height:1.65;color:#253c34">'
        f'<strong>{heading}</strong>　{escape(notes[heading])}</p>'
        for heading in ("走势解读", "标的概况") if notes.get(heading)
    )
    return f'''<a id="brief-{number}" name="brief-{number}"></a>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#ffffff" style="width:100%;border:1px solid #d8e1db"><tr><td class="stock-pad" style="padding:16px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td valign="middle"><h3 style="font-size:23px;line-height:1.35;margin:0;color:#15372d">{escape(row["name"])}</h3></td>
<td width="76" align="right" valign="middle" style="font-size:18px;line-height:1.4;color:#15634d;white-space:nowrap"><strong>{float(row["score"]):.1f}</strong> 分</td>
</tr></table>
<p style="margin:4px 0 12px;color:#52645f;font-size:14px;line-height:1.5">{identity}</p>
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#eef4ef"><tr><td style="padding:9px 12px;border-left:3px solid #37745d;font-size:16px;line-height:1.65">
<strong style="color:#214a3b">信号与风险</strong><br>{signal}</td></tr></table>
{paragraphs}{_stock_figures(card, number)}
<p style="margin:8px 0 0;text-align:right;font-size:14px"><a style="color:#36594c" href="#directory">返回候选名单</a></p>
</td></tr></table><p aria-hidden="true" style="margin:0;font-size:10px;line-height:10px">&nbsp;</p>'''


def render_html(snapshot, cards, report_day: date, minimum=DEFAULT_EMAIL_MIN_SCORE, *, preview=False, delayed=False, test=False):
    cutoff = str(snapshot["as_of"]).replace("T", " ")[:16] if snapshot else "暂无"
    alert = ("测试邮件" if test else "本地预览" if preview else "行情更新延迟" if delayed else
             "休市日报" if snapshot and cutoff[:10] != report_day.isoformat() else "盘后日报")
    directory, groups = [], []
    for eligible, anchor, title, color in (
        (True, "entries", "入场候选", "#14634c"),
        (False, "observations", "观察候选", "#3d587a"),
    ):
        members = [(i, card) for i, card in enumerate(cards, 1) if bool(card["row"].get("eligible")) == eligible]
        if not members:
            continue
        names = "　".join(
            f'<a href="#brief-{i}" style="display:inline-block;color:#244c41;text-decoration:underline">{escape(card["row"]["name"])}</a>'
            for i, card in members
        )
        directory.append(f'<p style="margin:5px 0;font-size:15px;line-height:1.8"><strong>{title} {len(members)} 只</strong>　{names}</p>')
        groups.append(f'<h2 id="{anchor}" style="margin:16px 0 8px;color:{color};font-size:20px;line-height:1.4">{title} · {len(members)}只</h2>')
        groups.extend(_stock_card(card, i) for i, card in members)

    if delayed:
        summary = "当天行情与快照尚未齐全，本次仅发送数据延迟通知。"
    elif not cards:
        summary = "本期没有符合评分门槛的候选。"
    else:
        summary = "".join(directory)
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Alpha Finance Radar · {report_day}</title>
<style>@media (max-width:480px){{.page-pad{{padding:6px 4px!important}}.stock-pad{{padding:12px 8px!important}}}}</style></head>
<body style="margin:0;padding:0;background:#f1f4f0;color:#233e34;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI','Microsoft YaHei',Arial,sans-serif">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" bgcolor="#f1f4f0" style="table-layout:fixed"><tr><td class="page-pad" align="center" style="padding:12px 6px">
<!--[if mso]><table role="presentation" width="680" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:680px;table-layout:fixed;text-align:left">
<tr><td bgcolor="#183d33" style="padding:16px 18px;color:#ffffff">
<p style="margin:0 0 6px;color:#d8e9df;font-size:14px;line-height:1.5">Alpha Finance Radar · {alert}</p>
<h1 style="margin:0;font-size:26px;line-height:1.35">盘后选股简报</h1>
<p style="margin:8px 0 0;color:#eef4f0;font-size:15px;line-height:1.6">{report_day}　·　评分 ≥ {minimum:g} 分</p></td></tr>
<tr><td id="directory" bgcolor="#e4ece6" style="padding:10px 16px;color:#213c32;font-size:16px;line-height:1.65">
<p style="margin:0;color:#496056;font-size:14px;line-height:1.6">数据截至 {escape(cutoff)}（上海时间）</p>
{summary}</td></tr>
<tr><td>{"".join(groups)}</td></tr>
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
