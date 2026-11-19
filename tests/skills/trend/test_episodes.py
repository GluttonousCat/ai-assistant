from copy import deepcopy
from datetime import datetime

import pandas as pd

from skills.trend import episodes
from tests.skills.trend.test_digest import row


def snapshot(day, *, eligible=False, origin="2026-11-12T14:00:00", version="v6", config=None, empty=False):
    candidate = row("600000.SH", score=80 if eligible else 65, eligible=eligible)
    candidate["trend60"]["origin"] = origin
    return {"run_id": day, "as_of": day + " 15:00:00", "version": version,
            "metadata": {"config": config or {"fresh_reversal": True}}, "data": [] if empty else [candidate]}


def test_repeated_selection_and_confirmation_keep_first_price_date_and_identity():
    source = [snapshot("2026-11-13"), snapshot("2026-11-16", eligible=True)]
    original = deepcopy(source)
    result = episodes.group_episodes(source)
    assert len(result) == 1
    first = result[0]
    assert first["first_seen"] == "2026-11-13" and first["last_seen"] == "2026-11-16"
    assert first["score"] == 65 and not first["eligible"]
    assert first["confirmed_at"] == "2026-11-16" and first["ever_eligible"]
    assert first["seen_count"] == 2 and source == original
    continued = episodes.group_episodes([*source, snapshot("2026-11-17", eligible=True)])
    assert continued[0]["episode_id"] == first["episode_id"]
    assert continued[0]["first_run_id"] == "2026-11-13"


def test_actual_absence_new_origin_or_changed_strategy_creates_a_new_round():
    for later in (snapshot("2026-11-16", origin="new"), snapshot("2026-11-16", version="v7"),
                  snapshot("2026-11-16", config={"fresh_reversal": True, "fast": 13})):
        assert len(episodes.group_episodes([snapshot("2026-11-13"), later])) == 2
    result = episodes.group_episodes([snapshot("2026-11-13"), snapshot("2026-11-16", empty=True), snapshot("2026-11-17")])
    assert len(result) == 2 and result[0]["episode_id"] != result[1]["episode_id"]


def test_missing_scan_day_or_derived_label_change_does_not_invent_a_new_signal():
    later = snapshot("2026-11-18")
    later["data"][0]["context_pattern"] = "base_rebound"
    result = episodes.group_episodes([later, snapshot("2026-11-13")])
    assert len(result) == 1 and result[0]["context_pattern"] == "trend_pullback"
    assert result[0]["observations"][-1]["context_pattern"] == "base_rebound"


def test_equivalent_origin_formats_do_not_create_daily_duplicate_rounds():
    source = [snapshot("2026-11-13", origin="2026-11-12 14:00:00.000"),
              snapshot("2026-11-16", origin="2026-11-12T06:00:00Z")]
    assert len(episodes.group_episodes(source)) == 1


def test_leaving_candidates_does_not_hide_losers_and_twenty_sessions_archive(monkeypatch):
    old = snapshot("2026-10-01")
    recent = snapshot("2026-11-13", origin="recent")
    monkeypatch.setattr(episodes, "tracking_history", lambda end: [snapshot("2026-11-16", empty=True), recent, old])
    calendar = [str(d.date()) for d in pd.bdate_range("2026-10-01", "2026-11-16")]
    monkeypatch.setattr(episodes, "tracking_calendar", lambda *a: calendar)

    def inputs(codes, start, end):
        return pd.DataFrame([{"ts_code": "600000.SH", "bar_end": day + " 15:00:00",
                              "close": 10 if day != "2026-11-16" else 9, "high": 10, "low": 9, "adj_factor": 1} for day in calendar])

    monkeypatch.setattr(episodes, "scan_daily_inputs", inputs)
    active = episodes.episode_tracking(now=datetime(2026, 11, 17, 10))
    archived = episodes.episode_tracking("archive", now=datetime(2026, 11, 17, 10))
    assert len(active["data"]) == len(archived["data"]) == 1
    assert active["data"][0]["first_seen"] == "2026-11-13"
    assert not active["data"][0]["in_latest_scan"]
    assert active["data"][0]["follow_up"]["latest_return_pct"] == -10.
    assert archived["data"][0]["age_sessions"] >= 20
