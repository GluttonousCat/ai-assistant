from concurrent.futures import ThreadPoolExecutor
import threading
from types import SimpleNamespace

from tools import monitoring as m


def test_concurrent_viewers_share_one_probe_and_cache_expires(monkeypatch):
    clock, calls = [100.0], []
    monkeypatch.setattr(m, "_cached", None)
    monkeypatch.setattr(m, "time", SimpleNamespace(monotonic=lambda: clock[0]))

    def read():
        calls.append(threading.get_ident())
        return {"last_trade_date": "2026-11-16"}

    monkeypatch.setattr(m, "database_state", read)
    with ThreadPoolExecutor(10) as pool:
        results = list(pool.map(lambda _: m.database_snapshot(), range(30)))
    assert len(calls) == 1 and all(r["ok"] for r in results)
    clock[0] = 109.9
    assert m.database_snapshot()["last_trade_date"] == "2026-11-16"
    assert len(calls) == 1
    clock[0] = 110.1
    m.database_snapshot()
    assert len(calls) == 2


def test_unavailable_database_is_a_cached_monitor_result(monkeypatch):
    monkeypatch.setattr(m, "_cached", None)
    calls = []

    def fail():
        calls.append(True)
        raise RuntimeError("unavailable")

    monkeypatch.setattr(m, "database_state", fail)
    assert m.database_snapshot() == {"ok": False, "error": "unavailable"}
    assert m.database_snapshot()["ok"] is False and len(calls) == 1
