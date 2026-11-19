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
            cid = f"stock-{code.replace('.', '-')}-{period.lower()}"
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
        trend.append(f"当日涨跌 {pct:+.2f}%")
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


def render_html(snapshot, cards, report_day: date, minimum=DEFAULT_EMAIL_MIN_SCORE, *, preview=False, delayed=False, test=False):
    e = lambda value: escape(str(value if value is not None else "—"))
    count = len(cards)
    cutoff = str(snapshot["as_of"]).replace("T", " ")[:16] if snapshot else "暂无"
    rows = [card["row"] for card in cards]
    entries = sum(bool(r.get("eligible")) for r in rows)
    pullbacks = sum(opportunity(r)[0] == TYPES["trend_pullback"][0] for r in rows)
    reversals = sum(opportunity(r)[0] == TYPES["base_rebound"][0] for r in rows)
    directory = []
    for eligible, title in ((True, "入场候选"), (False, "观察候选")):
        members = [(number, r) for number, r in enumerate(rows, 1) if bool(r.get("eligible")) == eligible]
        if not members:
            continue
        directory.append(f'<h3 style="font-size:15px;color:#dfb671;margin:20px 0 8px">{title}目录 · {len(members)}只</h3><table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;color:#d8cbbb;table-layout:fixed"><tr style="color:#8e8373;font-size:11px"><th align="left" width="48%" style="padding:8px">股票 / 板块</th><th align="left" width="36%">机会类型</th><th align="right" width="16%" style="padding:8px">评分</th></tr>')
        for number, r in members:
            label, color = opportunity(r)
            directory.append(f'<tr><td style="padding:10px 8px;border-bottom:1px solid #352f26"><a href="#stock-{number}" style="color:#f1e8db;text-decoration:none">{number:02d}　{e(r["name"])}</a><br><span style="color:#978b79;font-size:11px">{e(r["ts_code"])} · {e(r.get("board"))}</span></td><td style="color:{color};border-bottom:1px solid #352f26">{e(label)}</td><td align="right" style="padding:8px;color:#dfb671;border-bottom:1px solid #352f26">{float(r["score"]):.1f}</td></tr>')
        directory.append('</table>')
    entry_summary = f'入场候选 <strong>{entries}</strong>'
    observation_summary = f'观察候选 <strong>{len(cards)-entries}</strong>'
    if entries:
        entry_summary = f'<a href="#entries" style="color:#dfb671;text-decoration:none">{entry_summary}</a>'
    if len(cards) > entries:
        observation_summary = f'<a href="#observations" style="color:#b4a793;text-decoration:none">{observation_summary}</a>'
    sections = []
    previous_group = None
    for number, card in enumerate(cards, 1):
        r = card["row"]
        group = "入场候选" if r.get("eligible") else "观察候选"
        if group != previous_group:
            group_count = entries if r.get("eligible") else len(cards) - entries
            group_anchor = "entries" if r.get("eligible") else "observations"
            group_hint = "满足当前入场条件，先查看风险与K线结构" if r.get("eligible") else "尚未满足入场条件，留意后续确认"
            sections.append(f'<a id="{group_anchor}" name="{group_anchor}"></a><h2 style="font-size:23px;font-weight:500;color:#dfb671;margin:36px 0 6px">{group} · {group_count}只</h2><p style="font-size:12px;color:#958a79;margin:0 0 12px">{group_hint}</p>')
            previous_group = group
        label, color = opportunity(r)
        notes = "".join(
            f'<p style="font-size:14px;line-height:1.7;margin:8px 0;color:#d6c8b5">'
            f'<strong style="color:#dfb671">{e(title)}</strong>　{e(text)}</p>'
            for title, text in card["notes"].items()
        )
        charts = "".join(
            f'<tr><td style="padding:14px 8px 0"><strong style="color:#f1e8db;font-size:16px">{title}</strong>'
            f'<p style="color:#b8ab99;font-size:12px;margin:4px 0 8px">{caption}</p>'
            f'<img src="cid:{e(card["charts"][period]["cid"])}" width="936" alt="{e(r["name"])} {title}前复权K线" '
            'style="display:block;width:100%;max-width:100%;height:auto;border:0"></td></tr>'
            for period, (title, caption) in CHARTS.items()
        )
        sections.append(f'''<a id="stock-{number}" name="stock-{number}"></a><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:18px 0 28px;background:#171410;border:1px solid #3b3328;border-top:3px solid {color};border-radius:10px"><tr><td style="padding:18px 18px 10px">
<table role="presentation" width="100%"><tr><td><strong style="font-size:23px;color:#f1e8db">{number:02d}　{e(r['name'])}</strong><br><span style="font-size:12px;color:#b8ab99">{e(r['ts_code'])} · {e(r.get('board'))}</span></td><td align="right"><strong style="font-size:30px;color:#dfb671">{float(r['score']):.1f}</strong><br><span style="color:#aaa08f;font-size:12px">看板评分</span></td></tr></table>
<p style="font-size:14px;color:{color};margin:15px 0 8px">{e(label)}　<span style="color:#dfb671">{e(r.get('state'))}</span></p>
{notes}</td></tr>{charts}
<tr><td style="padding:12px 18px 18px"><a href="#digest-top" style="font-size:12px;color:#c8ab7e;text-decoration:none">↑ 返回目录</a></td></tr></table>''')
    alert = ("测试邮件 · 使用最近已发布快照" if test else "预览文件 · 尚未发送" if preview else "数据更新延迟 · 暂缓发送选股名单" if delayed else
             "休市日报 · 沿用最近完整交易日数据" if snapshot and cutoff[:10] != report_day.isoformat() else "每日18:00 · 盘后趋势观察")
    empty = '<p style="padding:30px 0;color:#b8ab99">当天行情与快照尚未完整更新；不会将旧名单冒充今日结果。完成后将补发正式日报。</p>' if delayed else '<p style="padding:30px 0;color:#b8ab99">本期没有符合评分门槛的候选，未降低阈值或扩大筛选范围。</p>' if not cards else ''
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Alpha Finance Radar · {report_day}</title></head>
<body style="margin:0;padding:0;background:#0c0b09;color:#d8cbbb;font-family:'Microsoft YaHei',Arial,sans-serif"><a id="digest-top" name="digest-top"></a><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0c0b09;table-layout:fixed"><tr><td align="center" style="padding:24px 12px">
<!--[if mso]><table role="presentation" width="960" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="width:100%;max-width:960px;table-layout:fixed;word-wrap:break-word"><tr><td>
<p style="color:#b79b70;font-size:11px;letter-spacing:3px;margin:0 0 16px">ALPHA FINANCE RADAR / DAILY DIGEST</p><h1 style="font-size:31px;color:#f2e8d9;font-weight:500;margin:0 0 10px">盘后趋势雷达</h1><p style="font-size:13px;color:#b39f80;margin:0 0 22px">{e(alert)} · 上海时间 {report_day}</p>
<table role="presentation" width="100%" cellpadding="12" cellspacing="0" style="border:1px solid #453824;background:#1d1811;table-layout:fixed"><tr><td width="27%"><span style="font-size:12px;color:#ac9b82">评分门槛</span><br><strong style="font-size:27px;color:#dfb671">&gt; {minimum:g}</strong></td><td width="27%"><span style="font-size:12px;color:#ac9b82">符合条件</span><br><strong style="font-size:27px;color:#f2e8d9">{count} 只</strong></td><td><span style="font-size:12px;color:#ac9b82">行情 / 策略截止</span><br><strong style="font-size:15px;color:#f2e8d9">{e(cutoff)}</strong></td></tr></table>
<table role="presentation" width="100%" cellpadding="12" cellspacing="0" style="background:#171410;font-size:13px"><tr><td style="color:#b4a793">{entry_summary}　{observation_summary}<br><span style="color:#80b8ff;line-height:2">↑ 趋势回调 {pullbacks}</span>　<span style="color:#c6a0f6">■ 底部反转 {reversals}</span></td></tr></table>
<p style="font-size:12px;color:#958a79;line-height:1.9">60F判断趋势，日K用于日级执行。观察候选与入场候选分开展示；评分用于排序，不是胜率或收益承诺。K线与前端使用同一快照、相同真实因子前复权和M8/13/21。</p>
{empty}{''.join(directory)}{''.join(sections)}
<p style="border-top:1px solid #332c22;padding-top:18px;font-size:11px;color:#817665;line-height:1.8">↑ 蓝色：趋势回调　■ 紫色：底部反转　▲ 日K入场标记　● 60F观察标记<br>本期股票及图表完整收录于此邮件，清单另附CSV。目录跳转取决于邮件客户端支持，也可按编号向下浏览。</p>
</td></tr></table><!--[if mso]></td></tr></table><![endif]--></td></tr></table></body></html>'''


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
