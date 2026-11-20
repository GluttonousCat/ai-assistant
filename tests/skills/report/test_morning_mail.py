from datetime import date, datetime, timedelta, timezone

from skills.report.morning_mail import needs_attachment, render_morning_mail


def edition(reports):
    day = date(2026, 11, 19)
    end = datetime(2026, 11, 19, 7, 30, tzinfo=timezone(timedelta(hours=8)))
    return {"issue_date": day, "window_start": end - timedelta(days=1), "window_end": end,
            "reports": reports, "total_count": len(reports)}


def report(number, *, readable=True):
    original = f"如果需求符合预期，利润率可能改善（样本{number}）。"
    claim = {"speaker": "公司管理层", "stance": "条件判断", "statement": original,
             "quote": original, "location": "PDF 第2页"}
    return {"report_id": number, "title": f"公司研究 #{number}", "org": "甲证券",
            "material_type": "original", "market": "A股", "target": "",
            "status": "verified" if readable else "pending", "partial": False,
            "claims": [claim] if readable else []}


def test_morning_email_quotes_are_exact_and_secondary_speakers_are_visible():
    row = dict(report(1), title='甲 & 乙 <script>', material_type="commentary", partial=True, analysis_version=3)
    row["claims"].append({"speaker": "调查受访者", "stance": "预期", "statement": "受访者预计需求或将回落。",
                          "quote": "Some respondents expect demand\nto soften.", "location": "片段3"})
    brief = edition([row])
    subject, plain, html = render_morning_mail(brief)
    assert "新增 1 篇" in subject and not needs_attachment(brief)
    for part in (plain, html):
        assert "11.18 07:30 至 11.19 07:30（北京时间）" in part
        assert "二手材料 · 涉及机构 甲证券" in part and "仅覆盖部分正文" in part
        assert "公司管理层 · 条件判断" in part and "调查受访者 · 预期" in part
        assert "PDF 第2页" in part and "片段3" in part
        assert part.count(row["claims"][0]["quote"]) == 1
        assert "历史中文转述" in part and "受访者预计需求或将回落。" in part
    assert "甲 &amp; 乙 &lt;script&gt;" in html and "<script>" not in html
    assert row["claims"][1]["quote"] in html and row["claims"][1]["quote"] in plain


def test_overflow_has_complete_plain_text_and_explicit_attachment_notice():
    rows = [report(i) for i in range(1, 32)]
    rows[6]["material_type"] = "unknown"
    rows[6]["status"] = "excerpt"
    rows[6]["claims"][0]["speaker"] = "归属待核实"
    brief = edition(rows)
    subject, plain, html = render_morning_mail(brief)
    assert needs_attachment(brief) and "随信的文本附件" in html and "新增 31 篇" in subject
    assert "材料来源待核实" in html and "归属待核实" in html
    assert rows[-1]["title"] not in html and rows[-1]["claims"][0]["quote"] in plain
    assert rows[6]["claims"][0]["quote"] not in html and rows[6]["claims"][0]["quote"] in plain


def test_empty_and_pending_days_have_distinct_copy_and_complete_small_directory():
    empty = edition([])
    assert "无新增研报" in render_morning_mail(empty)[0] and not needs_attachment(empty)
    pending = edition([report(1, readable=False)])
    subject, plain, html = render_morning_mail(pending)
    assert "新增 1 篇" in subject and "有原文摘录 0 篇" in plain and "待解析" in html
    assert "观点摘读" not in html and not needs_attachment(pending)


def test_preview_marks_an_open_window_and_never_changes_the_official_edition():
    brief = edition([])
    preview_at = brief["window_end"] - timedelta(hours=9)
    subject, plain, html = render_morning_mail({**brief, "preview_at": preview_at})
    assert "排版预览" in subject
    for part in (plain, html):
        assert "排版预览 · 截至 11.18 22:30，本期仍在收录" in part
    closed = render_morning_mail({**brief, "preview_at": brief["window_end"]})
    assert all("排版预览" in part and "仍在收录" not in part for part in closed)
    assert all("排版预览" not in part for part in render_morning_mail(brief))
