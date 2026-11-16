"""Regression tests for false freshness, stale responses and frozen scan rules."""
from dataclasses import asdict
from datetime import date, datetime
from types import SimpleNamespace

import pandas as pd
import pytest

from jobs import refresh_market as job
from tools.market import refresh_state


@pytest.fixture
def market(monkeypatch, tmp_path):
    monkeypatch.setattr(refresh_state, "_root", lambda: tmp_path / "output/market/refresh")
    monkeypatch.setattr(job, "get_config", lambda: SimpleNamespace(root_dir=tmp_path))
    monkeypatch.setattr(job, "scan_cutoffs", lambda now: {
        "bar_end": datetime(2026, 11, 12, 15), "factor_date": date(2026, 11, 13),
        "daily_end": date(2026, 11, 13),
    })
    frame = pd.DataFrame([{"ts_code": "600000.SH", "board": "主板", "list_date": date(2000, 1, 1)}])
    monkeypatch.setattr(job.store, "listed_universe", lambda: frame.to_dict("records"))
    monkeypatch.setattr(job.store, "save_listed_universe", lambda *a: None)
    monkeypatch.setattr("tools.market.universe.fetch_listed_universe", lambda *a: frame)
    monkeypatch.setattr(job.store, "daily_coverage", lambda *a: {
        "universe": 1, "active": ["600000.SH"], "daily_absent": [],
        "factor_absent": [], "basic_absent": [],
    })
    monkeypatch.setattr(job.store, "flag_intraday_gaps", lambda *a: None)
    return tmp_path


def audit(gaps):
    return {"counts": {"rows": 16}, "gaps_against_daily": gaps, "reference_gaps": [], "invalid_grid": 0}


def test_nonempty_but_stale_minutes_cannot_publish(market, monkeypatch):
    gaps = [{"ts_code": "600000.SH", "missing_dates": [date(2026, 11, 13)]}]
    monkeypatch.setattr(job.store, "listed_audit", lambda *a: audit(gaps))
    monkeypatch.setattr(job, "sync_one", lambda *a: {"status": "ok", "rows": 16})
    published = []
    monkeypatch.setattr(job, "publish_snapshot", lambda *a: published.append(a))
    result = job.refresh(date(2026, 11, 13), quotes=False)
    assert result["status"] == "error" and result["missing_60F"] == 1
    assert published == []
    assert refresh_state.read_state()["status"] == "error"


def test_completed_coverage_resumes_without_fetching_symbols(market, monkeypatch):
    monkeypatch.setattr(job.store, "listed_audit", lambda *a: audit([]))
    monkeypatch.setattr(job, "sync_one", lambda *a: pytest.fail("complete symbols must not refetch"))
    monkeypatch.setattr(job, "publish_snapshot", lambda target, coverage, **kw: {"as_of": str(target)})
    result = job.refresh(date(2026, 11, 13), quotes=False)
    assert result["status"] == "complete" and result["total"] == 0
    assert result["last_success"] == "2026-11-13"


def test_unknown_daily_absence_does_not_become_a_suspension(market, monkeypatch):
    monkeypatch.setattr(job.store, "daily_coverage", lambda *a: {
        "universe": 1, "active": [], "daily_absent": ["600000.SH"],
        "factor_absent": [], "basic_absent": [],
    })
    monkeypatch.setattr("tools.market.constituents.full_day_suspensions", lambda *a: set())
    monkeypatch.setattr(job, "publish_snapshot", lambda *a: pytest.fail("incomplete daily data"))
    result = job.refresh(date(2026, 11, 13), quotes=False)
    assert result["status"] == "error" and result["coverage"]["daily_unexplained"] == 1


def test_os_lock_prevents_concurrent_cli_and_scheduler(market):
    with refresh_state.refresh_lock() as acquired:
        assert acquired
        assert job.refresh(date(2026, 11, 13), quotes=False)["status"] == "busy"
    with refresh_state.refresh_lock() as acquired:
        assert acquired


def test_new_quotes_keep_the_published_strategy_configuration(market, monkeypatch):
    from jobs import scan_trend
    from skills.trend.mtf import MtfConfig, version_for
    cfg = MtfConfig(fresh_reversal=True, reversal_cycle_reset=True, risk_setups=False)
    monkeypatch.setattr(job, "load_dashboard", lambda **kw: {"metadata": {"config": asdict(cfg)}})
    seen = []

    def scan(target, config, **kwargs):
        assert asdict(config) == asdict(cfg)
        return "2026-11-13T15:00:00", {"config": asdict(config)}, []

    monkeypatch.setattr(scan_trend, "scan", scan)
    monkeypatch.setattr(job, "publish", lambda *args: seen.append(args))
    job.publish_snapshot(date(2026, 11, 13), {})
    assert seen[0][2] == version_for(cfg)


def test_fresh_daily_maximum_cannot_hide_stale_minutes_or_snapshot(monkeypatch):
    monkeypatch.setattr(job, "read_state", lambda: {"status": "complete", "target": "2026-11-13"})
    monkeypatch.setattr(job, "scan_cutoffs", lambda now: {
        "daily_end": date(2026, 11, 13), "factor_date": date(2026, 11, 13),
        "bar_end": datetime(2026, 11, 12, 15),
    })
    monkeypatch.setattr(job, "load_dashboard", lambda **kw: {"as_of": "2026-11-12 15:00:00"})
    assert job.needs_refresh(date(2026, 11, 13))
