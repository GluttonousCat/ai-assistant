"""Snapshot-pinned, score-filtered visual digest; no new trading decisions."""
import csv
from datetime import date
from html import escape
from io import StringIO
import math

from skills.trend.service import stock_detail
from tools.market.chart_image import render_pair
from tools.market.universe import risk_name

TYPES = {"trend_pullback": ("↑ 趋势回调", "#80b8ff"), "base_rebound": ("■ 底部反转", "#c6a0f6")}


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


def select_stocks(snapshot, minimum=60., observations=True):
    cfg = snapshot["metadata"]["config"]
    selected = []
    for row in snapshot["data"]:
        try:
            score = float(row.get("score", 0))
        except (ValueError, TypeError):
            continue
        if not math.isfinite(score) or score < minimum or risk_name(row.get("name")):
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
    cards = []
    for row in rows:
        details = {}
        for period in ("1D", "60F"):
            cutoff = snapshot["metadata"].get("timeframes", {}).get(period, {}).get("as_of", snapshot["as_of"])
            detail = stock_detail(row["ts_code"], snapshot["metadata"]["start"], cutoff,
                                  snapshot["metadata"]["config"], period)
            if detail.get("issue") or not detail["data"] or str(detail["data"][-1]["bar_end"])[:10] != str(cutoff)[:10]:
                raise ValueError(f"{row['ts_code']} {period}图表数据不完整，日报未发送")
            if detail.get("price_basis") != "forward_asof":
                raise ValueError("日报图表必须使用与看板相同的真实因子前复权")
            details[period] = detail
        stop = row.get("stop_price")
        png = render_pair(details["1D"], details["60F"], stop=float(stop) if stop else None)
        cards.append({"row": row, "png": png, "cid": "stock-" + row["ts_code"].replace(".", "-")})
    return cards


def _value(value, suffix=""):
    return "—" if value is None else f"{float(value):.2f}{suffix}"


def render_html(snapshot, cards, report_day: date, minimum=60., *, part=1, total_parts=1,
                total_count=None, preview=False, delayed=False, test=False, first_number=1):
    e = lambda value: escape(str(value if value is not None else "—"))
    count = len(cards) if total_count is None else total_count
    part_label = f" · 第{part}/{total_parts}部分" if total_parts > 1 else ""
    scope = (f'<p style="font-size:16px;color:#dfb671">本封 {len(cards)} 只 · 本期共 {count} 只 · '
             f'第{first_number}—{first_number + len(cards) - 1}只</p>' if total_parts > 1 else "")
    count_label = "本封候选" if total_parts > 1 else "符合条件"
    cutoff = str(snapshot["as_of"]).replace("T", " ")[:16] if snapshot else "暂无"
    rows = [card["row"] for card in cards]
    table_rows = "".join(f'<tr><td style="padding:10px;border-bottom:1px solid #352f26">{e(r["name"])}<br><span style="color:#978b79;font-size:11px">{e(r["ts_code"])} · {e(r.get("board"))}</span></td><td style="color:{opportunity(r)[1]};border-bottom:1px solid #352f26">{e(opportunity(r)[0])}</td><td style="color:#dfb671;border-bottom:1px solid #352f26">{float(r["score"]):.1f}</td><td style="border-bottom:1px solid #352f26">{"入场候选" if r.get("eligible") else "观察候选"}</td></tr>' for r in rows)
    sections = []
    previous_group = None
    for card in cards:
        r = card["row"]
        group = "入场候选" if r.get("eligible") else "观察候选"
        if group != previous_group:
            number = sum(bool(c["row"].get("eligible")) == bool(r.get("eligible")) for c in cards)
            sections.append(f'<h2 style="font-size:21px;font-weight:500;color:#dfb671;margin:32px 0 8px">{group} · {number}只</h2>')
            previous_group = group
        label, color = opportunity(r)
        checks = " · ".join(("✓ " if c.get("pass", c.get("passed", False)) else "× ") + str(c.get("label", c.get("name", ""))) for c in r.get("checks", [])[:10])
        sections.append(f'''<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="margin:22px 0;background:#171410;border:1px solid #3b3328;border-radius:12px"><tr><td style="padding:22px 22px 10px">
<table role="presentation" width="100%"><tr><td><strong style="font-size:23px;color:#f1e8db">{e(r['name'])}</strong><br><span style="font-size:12px;color:#a19482">{e(r['ts_code'])} · {e(r.get('board'))} · {e(r.get('industry'))}</span></td><td align="right"><strong style="font-size:30px;color:#dfb671">{float(r['score']):.1f}</strong><br><span style="color:#aaa08f;font-size:12px">看板评分</span></td></tr></table>
<p style="font-size:14px;color:{color};margin:15px 0 8px">{e(label)}　<span style="color:#dfb671">{'入场候选' if r.get('eligible') else '观察候选'} · {e(r.get('state'))}</span></p>
<p style="color:#b8ab99;font-size:13px;line-height:1.8;margin:0">{e(r.get('parent_trend_label', '母趋势待识别'))}{' · 回调 ' + e(r['parent_pullback_bars']) + ' 根日K' if r.get('parent_pullback_bars') else ''}<br>截止价 {_value(r.get('metrics',{}).get('close'))}　参考止损 {_value(r.get('stop_price'))}　风险距离 {_value(r.get('risk_pct'), '%')}</p>
</td></tr><tr><td style="padding:6px 8px"><img src="cid:{card['cid']}" width="944" alt="{e(r['name'])} 日K与60F前复权K线，M8/13/21" style="display:block;width:100%;max-width:944px;height:auto;border:0"></td></tr>
<tr><td style="padding:12px 22px 20px"><p style="color:#b4a793;font-size:13px;line-height:1.8;margin:0">筛选解释：{e(r.get('reason'))}</p><p style="color:#8d8273;font-size:12px;line-height:1.7;margin:6px 0 0">{e(checks)}</p></td></tr></table>''')
    alert = ("测试邮件 · 使用最近已发布快照" if test else "预览文件 · 尚未发送" if preview else "数据更新延迟 · 暂缓发送选股名单" if delayed else
             "休市日报 · 沿用最近完整交易日数据" if snapshot and cutoff[:10] != report_day.isoformat() else "每日18:00 · 盘后趋势观察")
    empty = '<p style="padding:30px 0;color:#b8ab99">当天行情与快照尚未完整更新；不会将旧名单冒充今日结果。完成后将补发正式日报。</p>' if delayed else '<p style="padding:30px 0;color:#b8ab99">本期没有符合评分门槛的候选，未降低阈值或扩大筛选范围。</p>' if not cards else ''
    return f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Alpha Finance Radar · {report_day}</title></head>
<body style="margin:0;padding:0;background:#0c0b09;color:#d8cbbb;font-family:'Microsoft YaHei',Arial,sans-serif"><table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#0c0b09"><tr><td align="center" style="padding:28px 10px"><table role="presentation" width="980" cellpadding="0" cellspacing="0" style="width:100%;max-width:980px"><tr><td>
<p style="color:#b79b70;font-size:11px;letter-spacing:3px;margin:0 0 16px">ALPHA FINANCE RADAR / DAILY DIGEST</p><h1 style="font-size:31px;color:#f2e8d9;font-weight:500;margin:0 0 10px">盘后趋势雷达</h1><p style="font-size:13px;color:#b39f80;margin:0 0 22px">{e(alert)} · 上海时间 {report_day}{part_label}</p>{scope}
<table role="presentation" width="100%" cellpadding="16" cellspacing="0" style="border:1px solid #453824;background:#1d1811"><tr><td><span style="font-size:12px;color:#ac9b82">评分门槛</span><br><strong style="font-size:27px;color:#dfb671">≥ {minimum:g}</strong></td><td><span style="font-size:12px;color:#ac9b82">{count_label}</span><br><strong style="font-size:27px;color:#f2e8d9">{len(cards)} 只</strong></td><td><span style="font-size:12px;color:#ac9b82">行情 / 策略截止</span><br><strong style="font-size:17px;color:#f2e8d9">{e(cutoff)}</strong></td></tr></table>
<p style="font-size:12px;color:#958a79;line-height:1.9">60F判断趋势，日K用于日级执行。观察候选与入场候选分开展示；评分用于排序，不是胜率或收益承诺。K线与前端使用同一快照、相同真实因子前复权和M8/13/21。</p>
{empty}<table width="100%" cellpadding="0" cellspacing="0" style="font-size:13px;color:#d8cbbb">{table_rows}</table>{''.join(sections)}
<p style="border-top:1px solid #332c22;padding-top:18px;font-size:11px;color:#817665;line-height:1.8">↑ 蓝色：趋势回调　■ 紫色：底部反转　▲ 日K入场标记　● 60F观察标记<br>股票清单随信附CSV；若本期分多部分发送，每部分均保留明确的截止日期。</p>
</td></tr></table></td></tr></table></body></html>'''


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
