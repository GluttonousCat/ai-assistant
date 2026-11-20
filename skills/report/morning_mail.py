"""Render the 07:30 report brief for HTML and plain-text mail clients."""
from html import escape


# The HTML is the morning read. The UTF-8 text part doubles as the complete
# attachment on busy days, keeping the body within mail-client clipping limits.
_FEATURED = 6
_CONTENTS = 20
_SANS = "Arial,'Microsoft YaHei','PingFang SC',sans-serif"
_SERIF = "Georgia,'Songti SC','Noto Serif CJK SC',SimSun,serif"
_STATUS = {
    "verified": "有原文依据", "excerpt": "原文摘录 · 发言人待核实",
    "pending": "待解析", "legacy": "历史分析待核对", "unavailable": "暂无可核对原文",
}
_MATERIAL = {"original": "原研报", "commentary": "二手材料", "unknown": "材料来源待核实"}


def _selection(reports):
    featured, contents = [], []
    for report in reports:
        if len(featured) < _FEATURED and report["claims"]:
            featured.append(report)
        else:
            contents.append(report)
    return featured, contents


def needs_attachment(brief):
    """True when HTML omits a claim or a report that is present in the text."""
    _, contents = _selection(brief["reports"])
    return len(contents) > _CONTENTS or any(report["claims"] for report in contents)


def _identity(report):
    material = report["material_type"]
    org = report.get("org")
    if org and material != "original":
        org = f"涉及机构 {org}"
    parts = [_MATERIAL.get(material, _MATERIAL["unknown"]), org, report.get("market"), report.get("target")]
    return " · ".join(dict.fromkeys(str(p) for p in parts if p))


def _status(report):
    status = report.get("pending_reason") or _STATUS.get(report["status"], _STATUS["unavailable"])
    return f"{status} · 仅覆盖部分正文" if report["partial"] else status


def _summary_line(claim):
    """Only a distinct, checked translation belongs above the original excerpt."""
    statement = claim["statement"] or ""
    return statement if statement.split() != claim["quote"].split() else ""


def _label(claim):
    if claim["speaker"] == "归属待核实" and claim["stance"] == "语气待核实":
        return "发言人与语气待核实"
    return f'{claim["speaker"]} · {claim["stance"]}'


def _translation_label(report):
    return "历史中文转述" if report.get("analysis_version", 4) < 4 else "中文转述"


def _report_text(report):
    lines = [report["title"], f'{_identity(report)}  |  研报 #{report["report_id"]}  |  {_status(report)}']
    for claim in report["claims"]:
        lines.extend(["", _label(claim)])
        if translated := _summary_line(claim):
            lines.append(_translation_label(report) + "  " + translated)
        lines.extend(["原文  ·  " + claim["location"], claim["quote"]])
    return "\n".join(lines)


def _claim_html(claim, report):
    translated = _summary_line(claim)
    text = (f'<p style="margin:5px 0 9px;font-size:16px;line-height:1.72;color:#203c35">'
            f'<span style="font-size:12px;color:#54685f">{_translation_label(report)}　</span>{escape(translated)}</p>') if translated else ""
    # Ignore PDF soft line endings in the fluid layout. The text part retains
    # every original line break; paragraph boundaries remain visible in HTML.
    quote = escape(claim["quote"]).replace("\n\n", "<br><br>")
    return f'''<div style="margin:14px 0 0">
<p style="margin:0;color:#296052;font-size:13px;line-height:1.6;font-weight:bold">{escape(_label(claim))}</p>
{text}<div style="margin-top:5px;padding:9px 14px 11px;border-left:3px solid #a88a52;background-color:#f5f3eb">
<p style="margin:0 0 3px;font-size:12px;line-height:1.6;color:#6a5937">原文摘录　·　{escape(claim["location"])}</p>
<p style="margin:0;font-size:15px;line-height:1.65;color:#334a40">{quote}</p></div></div>'''


def _report_html(report):
    notes = [f'研报 #{report["report_id"]}']
    if report["partial"]:
        notes.append("仅覆盖部分正文")
    if report.get("analysis_version", 4) < 4:
        notes.append("历史引文已与原件核对")
    return f'''<tr><td class="pad" style="padding:18px 36px 24px;border-bottom:1px solid #d7dfd6">
<p style="margin:0 0 5px;font-size:12px;line-height:1.65;color:#50655a">{escape(_identity(report))}</p>
<h3 style="margin:0;font-family:{_SERIF};font-size:22px;line-height:1.45;color:#17392e">{escape(report["title"])}</h3>
<p style="margin:4px 0 0;font-size:12px;line-height:1.65;color:#637469">{escape(" · ".join(notes))}</p>
{"".join(_claim_html(claim, report) for claim in report["claims"])}</td></tr>'''


def _contents_html(report):
    labels = [f'#{report["report_id"]}', _identity(report)]
    if report["claims"]:
        labels.append("、".join(dict.fromkeys(claim["speaker"] for claim in report["claims"])))
    labels.append(_status(report))
    return f'''<tr><td class="pad" style="padding:10px 36px 12px;border-bottom:1px solid #e3e7dd">
<p style="margin:0;font-size:15px;line-height:1.55;font-weight:bold;color:#30493c">{escape(report["title"])}</p>
<p style="margin:3px 0 0;font-size:12px;line-height:1.65;color:#5d6e62">{escape(" · ".join(labels))}</p></td></tr>'''


def _section(title, note):
    return f'''<tr><td class="pad" style="padding:22px 36px 10px;background-color:#fafaf5">
<h2 style="margin:0;font-family:{_SERIF};font-size:24px;line-height:1.45;color:#17392e">{title}</h2>
<p style="margin:4px 0 0;color:#586b5e;font-size:13px;line-height:1.65">{note}</p></td></tr>'''


def render_morning_mail(brief):
    """Return (subject, complete plain text, responsive HTML) for one issue."""
    day = brief["issue_date"]
    date_label = f'{day:%Y.%m.%d}　星期{"一二三四五六日"[day.weekday()]}'
    window = f'{brief["window_start"]:%m.%d %H:%M} 至 {brief["window_end"]:%m.%d %H:%M}（北京时间）'
    reports = brief["reports"]
    featured, contents = _selection(reports)
    total = brief["total_count"]
    readable = sum(bool(r["claims"]) for r in reports)
    summary = f"新入库 {total} 篇，有原文摘录 {readable} 篇。" if total else "过去 24 小时没有新入库的研报。"
    issue_count = f"新增 {total} 篇" if total else "无新增研报"
    subject = f"研报观点速报｜{day:%m月%d日} · {issue_count}"
    preview_at = brief.get("preview_at")
    preview_label = ""
    still_collecting = preview_at is not None and preview_at < brief["window_end"]
    if preview_at is not None:
        preview_label = "今日样刊" if brief.get("sample") else "排版预览"
        subject = f"【{preview_label}】" + subject
        if still_collecting:
            preview_label += f" · 截至 {preview_at:%m.%d %H:%M}，本期仍在收录"
            summary = summary if total else "截至预览时，本期尚无新入库的研报。"
    plain = [f"研报观点速报　{date_label}", "统计时段　" + window, summary]
    if preview_label:
        plain.insert(1, preview_label)
    if total:
        plain.extend(_report_text(r) for r in reports)
    text = "\n\n".join(plain) + "\n"

    body = []
    if featured:
        body.append(_section("观点摘读", f"选读 {len(featured)} 篇 · 判断与原文对照"))
        body.extend(_report_html(r) for r in featured)
    if contents:
        note = (f"另有 {len(contents)} 篇，列出其中 {min(len(contents), _CONTENTS)} 篇。"
                if featured else f"本期 {total} 篇均待核对，先列出报告与处理进度。")
        body.append(_section("入库目录", note))
        body.extend(_contents_html(r) for r in contents[:_CONTENTS])
    if not reports:
        empty = "后续入库的研报将计入本期正式邮件。" if still_collecting else "上期截止后，研报库还没有新的入库记录。"
        body.append(f'''<tr><td class="pad" style="padding:32px 36px 36px">
<h2 style="margin:0 0 6px;font-size:22px;color:#193c30">本期无新增研报</h2>
<p style="margin:0;color:#53645a;font-size:15px;line-height:1.7">{empty}</p></td></tr>''')
    if needs_attachment(brief):
        body.append(f'''<tr><td class="pad" style="padding:16px 36px;background-color:#eef2e9">
<p style="margin:0;color:#355543;font-size:14px;line-height:1.7">本期 {total} 篇的完整清单、收录观点和原文见随信的文本附件。</p></td></tr>''')

    stat_html = ""
    if total:
        stats = [(total, "本期入库"), (readable, "含原文摘录"), (total - readable, "待核对原文")]
        cells = "".join(f'''<td width="33%" valign="top" style="padding:0 5px 0 0">
<p style="margin:0;font-size:12px;line-height:1.55;color:#526454">{label}</p>
<p style="margin:3px 0 0;color:#203d30;font-family:{_SERIF};font-size:27px;line-height:1.15">{count}
<span style="font:12px {_SANS};color:#526454">篇</span></p></td>''' for count, label in stats)
        stat_html = f'''<tr><td class="pad" bgcolor="#eef1e7" style="padding:14px 36px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>{cells}</tr></table></td></tr>'''

    preview_html = (f'<p style="margin:8px 0 0;color:#f2dfa9;font-size:13px;line-height:1.65">{preview_label}</p>'
                    if preview_label else "")
    html = f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(subject)}</title>
<style>@media only screen and (max-width:520px){{.frame{{padding:0!important}}.pad{{padding-left:19px!important;padding-right:19px!important}}.mast{{font-size:32px!important}}.brand{{letter-spacing:1px!important}}}}</style></head>
<body style="margin:0;padding:0;background-color:#e9ede6;font-family:{_SANS};color:#263d30">
<div style="display:none;max-height:0;overflow:hidden;mso-hide:all">{summary}</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#e9ede6" style="table-layout:fixed">
<tr><td class="frame" align="center" style="padding:24px 10px 32px">
<!--[if mso]><table role="presentation" width="660" cellpadding="0" cellspacing="0"><tr><td><![endif]-->
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" bgcolor="#ffffff"
 style="width:100%;max-width:660px;table-layout:fixed;overflow-wrap:anywhere;word-wrap:break-word;text-align:left;font-family:{_SANS}">
<tr><td class="pad" bgcolor="#183b30" style="padding:22px 36px 25px;border-top:4px solid #bd9e61;color:#fffaf0">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>
<td class="brand" style="padding-bottom:12px;color:#efe6c7;font-family:Georgia,serif;font-weight:bold;font-size:11px;line-height:1.65;letter-spacing:1.7px">ALPHA FINANCE RADAR</td>
<td align="right" style="padding-bottom:12px;color:#e9eadc;font-size:12px;line-height:1.65">{date_label}</td></tr></table>
<h1 class="mast" style="margin:0;padding:16px 0 9px;border-top:1px solid #537063;color:#fff9eb;font-family:{_SERIF};font-size:40px;line-height:1.2">研报观点速报</h1>
<p style="margin:0;color:#dfebde;font-size:13px;line-height:1.7">{window}</p>{preview_html}</td></tr>
{stat_html}{"".join(body)}
</table><!--[if mso]></td></tr></table><![endif]--></td></tr></table></body></html>'''
    return subject, text, html
