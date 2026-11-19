"""Text-first midday brief shared by previews and scheduled delivery."""
import csv
from html import escape
from io import StringIO

from skills.trend.digest import opportunity
from skills.trend.intraday_followup import build_followup, new_observations


def _tracking_line(row):
    events = "、".join(row["events"]) or row["state"]
    parts = [f"{row['name']} {row['ts_code']}", events, f"上次入选 {row['last_seen']}"]
    if row.get("previous_state"):
        parts[-1] += f" {row['previous_state']}"
    if row["change_pct"] is not None:
        parts.append(f"较昨收 {row['change_pct']:+.2f}%")
    if row["support"] is not None:
        parts.append(f"历史参考位 {row['support']:.2f}")
    return " · ".join(parts)


def _observation_line(row):
    label, _ = opportunity(row)
    origin = (row.get("trend60") or {}).get("origin")
    clock = str(origin).replace("T", " ")[11:16] if origin else ""
    parts = [f"{row['name']} {row['ts_code']}", label, f"{row['score']:g}/80 分"]
    if clock:
        parts.append(f"起点 {clock}")
    if row.get("reason"):
        parts.append(row["reason"])
    return " · ".join(parts)


def render(report):
    tracked = report.get("follow_up")
    if tracked is None:
        tracked = build_followup(report)
    observations = new_observations(report)
    changes = [row for row in tracked if row["events"]]
    unknown = sum(not row.get("scan_verified", True) for row in tracked)
    headline = "午间 60F 简报"
    cutoff = f"60F 截至 {report['as_of']} · 日 K 背景截至 {report['daily_as_of']}"
    summary = f"跟踪 {len(tracked)} 只，记录变化 {len(changes)} 只，逐股扫描结果待核验 {unknown} 只。"
    prior_lines = [_tracking_line(row) for row in changes] or ["本轮暂无已记录的信号变化。"]
    new_lines = [_observation_line(row) for row in observations] or ["本轮暂无符合条件的当日新观察。"]
    footnote = "观察信号等待完整日 K 确认。附件含所有跟踪标的和新观察清单。"
    sections = (("历史标的跟踪", summary, prior_lines), ("盘中新观察", f"当日新起点 {len(observations)} 只", new_lines))
    text = "\n".join([headline, cutoff, *[part for title, line, items in sections for part in (title, line, *items)], footnote])
    body = "".join(
        f'<h2 style="font-size:19px;margin:22px 0 6px">{title}</h2><p>{escape(summary)}</p>'
        + "".join(f'<p style="border-top:1px solid #e0e7ef;padding:10px 0;margin:0">{escape(line)}</p>' for line in lines)
        for title, summary, lines in sections
    )
    html = f'''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<body style="margin:0;background:#edf2f7;color:#19344e;font-family:'Microsoft YaHei',Arial,sans-serif;font-size:14px;line-height:1.65">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0"><tr><td align="center" style="padding:12px">
<table role="presentation" width="100%" cellspacing="0" cellpadding="0" style="max-width:860px;background:#fff;border-top:4px solid #1c5680">
<tr><td style="padding:16px"><h1 style="margin:0;font-size:26px">{headline}</h1><p>{escape(cutoff)}</p>
{body}<p style="color:#415a70">{footnote}</p></td></tr></table></td></tr></table></body></html>'''
    csv_file = StringIO(newline="")
    writer = csv.writer(csv_file)
    writer.writerow(["分组", "代码", "名称", "信号状态", "备注"])
    for group, rows in (("历史跟踪", tracked), ("盘中新观察", observations)):
        for row in rows:
            fields = [group, row["ts_code"], row["name"], row["state"],
                      "、".join(row["events"]) if group == "历史跟踪" else row.get("reason", "")]
            writer.writerow([f"'{value}" if str(value).startswith(("=", "+", "-", "@")) else value
                             for value in fields])
    return html, text, csv_file.getvalue(), {"tracked": len(tracked), "changes": len(changes), "new": len(observations)}
