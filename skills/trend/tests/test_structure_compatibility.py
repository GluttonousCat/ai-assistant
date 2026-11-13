from dataclasses import asdict

from skills.trend import service
from skills.trend.strategy import TrendConfig
from skills.trend.tests.test_startup import v2_rebound_sample


def test_old_snapshot_keeps_v2_signals_while_new_snapshot_filters_them(monkeypatch):
    frame, seed = v2_rebound_sample()
    monkeypatch.setattr(service, "adjusted_bars", lambda *a: frame.copy())
    old = asdict(TrendConfig(selection_mode="early"))
    old.pop("require_precursor")
    old.pop("precursor_min_signs")
    before = service.stock_detail("601169.SH", "2026-01-01", "2026-11-12T15:00:00", old)
    after = service.stock_detail(
        "601169.SH",
        "2026-01-01",
        "2026-11-12T15:00:00",
        asdict(TrendConfig(selection_mode="early")),
    )
    assert before["data"][seed]["first_watch"]
    assert not after["data"][seed]["first_watch"]
