from datetime import datetime

import pandas as pd

from skills.trend import episodes
from skills.trend import intraday_followup as followup
from skills.trend.episode_support import bars_by_close, support_exit
from skills.trend.intraday_followup import apply_followup, visible_followup
from skills.trend.intraday_digest import render
from tests.skills.trend.test_episodes import snapshot


CODE = "600000.SH"
DAYS = ["2026-11-17", "2026-11-18", "2026-11-19"]
FIRST, SECOND, THIRD = (DAYS[1] + " " + t for t in ("10:30:00", "11:30:00", "14:00:00"))


def candle(stamp, close, factor, volume=100):
    return {"ts_code": CODE, "bar_end": stamp, "close": close, "high": close, "low": close,
            "adj_factor": factor, "volume": volume}


def test_two_completed_adjacent_closes_confirm_once_and_only_after_publication():
    ep = {"ts_code": CODE, "first_seen": DAYS[0], "stop_price": 20}
    daily = bars_by_close(pd.DataFrame([candle(DAYS[0] + " 15:00:00", 21, 1)]))
    minute = bars_by_close(pd.DataFrame([
        candle(DAYS[0]+" 14:00:00", 10, 1), candle(DAYS[0]+" 15:00:00", 10, 1),
        candle(FIRST, 10, 2), candle(SECOND, 9.9, 2), candle(THIRD, 9.8, 2),
        candle(DAYS[1]+" 15:00:00", 11, 2)]))
    assert support_exit(ep, DAYS, SECOND, daily, minute) is None
    assert support_exit(ep, DAYS, THIRD, daily, minute) == THIRD
    assert support_exit(ep, DAYS, DAYS[2] + " 15:00:00", daily, minute) == THIRD
    published_after_second = {**ep, "first_published_at": DAYS[1]+"T03:45:00+00:00"}
    assert support_exit(published_after_second, DAYS, THIRD, daily, minute) is None
    for bad in [{}, candle(SECOND, 9.9, None), candle(SECOND, 9.9, 2, volume=0)]:
        minute[CODE, SECOND] = bad
        assert support_exit(ep, DAYS, THIRD, daily, minute) is None


def test_tracking_archives_broken_structure_preserves_performance_and_allows_fresh_origin(monkeypatch):
    old = snapshot(DAYS[0])
    old["data"][0]["stop_price"] = 20
    new = snapshot(DAYS[2], origin=DAYS[2]+"T14:00:00")
    new["data"][0]["stop_price"] = 18
    daily = pd.DataFrame([candle(d+" 15:00:00", close, factor) for d, close, factor in
                          zip(DAYS, [22, 9, 11], [1, 2, 2])])
    minute = pd.DataFrame([candle(FIRST, 9.5, 2), candle(SECOND, 9.2, 2), candle(THIRD, 10.5, 2)])
    monkeypatch.setattr(episodes, "tracking_history", lambda _: [old, new])
    monkeypatch.setattr(episodes, "tracking_calendar", lambda start, end: DAYS)
    monkeypatch.setattr(episodes, "scan_daily_inputs", lambda *args: daily)
    monkeypatch.setattr(episodes, "scan_inputs", lambda *args: minute)
    now = datetime(2026, 11, 19, 18)
    active = episodes.episode_tracking(now=now)
    archived = episodes.episode_tracking("archive", now=now)
    assert active["counts"] == archived["counts"] == {"active": 1, "archive": 1}
    assert active["data"][0]["first_seen"] == DAYS[2]
    assert archived["data"][0]["retired_at"] == SECOND
    assert archived["data"][0]["follow_up"]["latest_return_pct"] == 0

    monkeypatch.setattr(episodes, "tracking_history", lambda end: [old] if end[:10] < DAYS[2] else [old, new])
    kwargs = {"config": old["metadata"]["config"], "version": old["version"]}
    assert episodes.retired_signals(old["data"], SECOND, **kwargs) == {CODE}
    assert not episodes.retired_signals(new["data"], SECOND, **kwargs)
    assert not episodes.retired_signals(old["data"], SECOND, **{**kwargs, "version": "next"})
    assert not episodes.retired_signals(old["data"], FIRST, **kwargs)


def test_retirement_filters_only_same_origin_and_morning_brief_retains_first_slot_exit():
    candidate = {"ts_code": CODE, "name": "甲", "trend60": {"origin": "2026-11-17T10:30:00"}}
    broken = {"ts_code": CODE, "name": "甲", "state": "已移出跟踪", "retired_at": FIRST,
              "origin": candidate["trend60"]["origin"], "last_seen": DAYS[0],
              "events": [], "support": 10, "change_pct": None}
    report = {"as_of": SECOND, "daily_as_of": DAYS[0]+" 15:00:00", "data": [candidate],
              "scan_status": {CODE: "candidate"}, "statuses": {"candidate": 1}}
    apply_followup(report, [broken])
    assert report["data"] == [] and report["scan_status"][CODE] == "filtered"
    assert report["statuses"] == {"candidate": 0, "filtered": 1}
    _, text, csv, counts = render(report)
    assert "早盘移出 1 只" in text and "10:30 连续两根60F" in text
    assert counts["changes"] == 1 and "早盘移出" in csv
    assert not visible_followup([broken], DAYS[2]+" 10:30:00")
    fresh = {**candidate, "trend60": {"origin": THIRD}}
    report["data"] = [fresh]
    apply_followup(report, [broken])
    assert report["data"] == [fresh]


def test_first_slot_compares_to_previous_session_and_ignores_later_backfill(monkeypatch):
    source = snapshot(DAYS[0])
    source["data"][0]["stop_price"] = 20
    config = source["metadata"]["config"]
    cutoff = DAYS[1] + " 10:30:00"
    prior = {"as_of": DAYS[0] + " 15:00:00", "data": source["data"], "strategy_version": "v6", "config": config}
    report = {"as_of": cutoff, "daily_as_of": prior["as_of"], "source_snapshot_run_id": source["run_id"],
              "slots": ["10:30"], "config": config, "strategy_version": "v6", "data": [], "scan_status": {CODE: "filtered"}}
    later_backfill = snapshot(DAYS[0], origin=DAYS[0]+"T14:00:00")
    later_backfill.update(run_id="late", created_at=DAYS[1]+"T03:30:00Z")
    monkeypatch.setattr(followup, "tracking_history", lambda _: [source, later_backfill])
    monkeypatch.setattr(followup, "tracking_calendar", lambda *a: DAYS[:2])
    monkeypatch.setattr(followup, "intraday_report", {DAYS[0]+"_1500": prior}.get)
    monkeypatch.setattr(followup, "scan_daily_inputs", lambda *a: pd.DataFrame([candle(prior["as_of"], 22, 1)]))
    monkeypatch.setattr(followup, "scan_inputs", lambda *a: pd.DataFrame([candle(cutoff, 9.8, 2)]))
    result, = followup.build_followup(report)
    assert result["origin"] == source["data"][0]["trend60"]["origin"]
    assert result["tracking_status"] == "active" and result["support"] == 10
    assert result["events"] == ["跌破历史参考位", "退出盘中观察"]
    report["scan_status"][CODE] = "data_missing"
    result, = followup.build_followup(report)
    assert result["tracking_status"] == "active" and not result["events"] and not result["scan_verified"]
