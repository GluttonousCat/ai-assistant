"""Transport parsing and supplier-data validation without network calls."""
import json
from types import SimpleNamespace
import pytest

from tools.market.sina_transport import fetch_sina_history


def test_transport_uses_only_one_request_and_preserves_share_units(monkeypatch):
    calls = []
    row = {"day": "2026-11-11 15:00:00", "open": "10", "high": "11", "low": "9",
           "close": "10.5", "volume": "15000", "amount": "150000"}

    def get(url, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(text="callback=(" + json.dumps([row]) + ");", raise_for_status=lambda: None)

    monkeypatch.setattr("tools.market.sina_transport.requests.get", get)
    frame = fetch_sina_history("600000.SH", "2026-01-01", "2026-11-11")
    assert frame.iloc[0].volume == 15000
    assert len(calls) == 1
    assert calls[0]["params"]["scale"] == "60"
    assert calls[0]["timeout"] == (5, 15)


def test_transport_rejects_invalid_ohlc(monkeypatch):
    row = {"day": "2026-11-11 15:00:00", "open": "10", "high": "9", "low": "8",
           "close": "10.5", "volume": "15000"}
    monkeypatch.setattr("tools.market.sina_transport.requests.get", lambda *a, **k:
                        SimpleNamespace(text="callback=("+json.dumps([row])+");", raise_for_status=lambda: None))
    with pytest.raises(ValueError, match="OHLC"):
        fetch_sina_history("600000.SH", "2026-01-01", "2026-11-11")


def test_bj_constituents_are_requested_with_the_bj_prefix(monkeypatch):
    seen = []
    monkeypatch.setattr("tools.market.sina_transport.requests.get", lambda *a, **kw:
                        (seen.append(kw["params"]["symbol"]) or
                         SimpleNamespace(text="=([]);", raise_for_status=lambda: None)))
    frame = fetch_sina_history("920001.BJ", "2026-01-01", "2026-11-11")
    assert frame.empty
    assert seen == ["bj920001"]


def test_intraday_collection_can_request_a_small_window(monkeypatch):
    seen = []
    monkeypatch.setattr("tools.market.sina_transport.requests.get", lambda *a, **kw:
                        seen.append(kw["params"]["datalen"]) or SimpleNamespace(text="=([]);", raise_for_status=lambda: None))
    fetch_sina_history("600000.SH", "2026-11-17", "2026-11-17", limit=8)
    assert seen == ["8"]
    with pytest.raises(ValueError):
        fetch_sina_history("600000.SH", "2026-11-17", "2026-11-17", limit=0)
