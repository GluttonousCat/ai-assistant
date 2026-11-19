from skills.trend.intraday_followup import _change, _price_context, new_observations


def test_absence_and_missing_do_not_read_as_lost_signal():
    assert _change("candidate", None, {}, None) is None
    assert _change("candidate", "data_missing", {}, None) is None
    assert _change("candidate", "filtered", {}, None) == "退出盘中观察"
    assert _change("filtered", "candidate", {}, {}) == "转入盘中观察"
    old = {"trend60": {"origin": "2026-11-19T10:30:00"}}
    new = {"trend60": {"origin": "2026-11-19 11:30:00"}}
    assert _change("candidate", "candidate", old, new) == "60F 结构起点更新"


def test_ex_rights_only_marks_support_crossing_after_moving_to_same_factor():
    ticker = "600000.SH"
    previous, morning, cutoff = "2026-11-18 15:00:00", "2026-11-19 10:30:00", "2026-11-19 11:30:00"
    daily = {(ticker, previous): {"close": 20, "adj_factor": 1}}
    minute = {(ticker, cutoff): {"close": 9.5, "adj_factor": 2}, (ticker, morning): {"close": 10, "adj_factor": 2}}
    change, close, level, crossed = _price_context(ticker, previous[:10], 19, daily, minute, previous, cutoff, morning)
    assert (change, close, level, crossed) == (-5, 9.5, 9.5, False)
    minute[ticker, cutoff]["close"] = 9.4
    assert _price_context(ticker, previous[:10], 19, daily, minute, previous, cutoff, morning)[-1]
    assert new_observations({"data": [{"new_today": False}, {"new_today": True}]}) == [{"new_today": True}]
