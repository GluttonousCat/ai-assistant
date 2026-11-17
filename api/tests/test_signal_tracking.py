from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.market_api import market_router
from api.middleware import AuthMiddleware


def client(auth=False):
    app = FastAPI()
    app.include_router(market_router)
    if auth:
        app.add_middleware(AuthMiddleware)
    return TestClient(app)


@pytest.mark.parametrize("path", ["/tracking", "/tracking/episodes", "/tracking/600000.SH?run_id=410395ca-57cc-4f10-bd62-4aeb023f223b"])
def test_tracking_is_authenticated(path):
    assert client(auth=True).get("/api/v1/market" + path).status_code == 401


def test_tracking_validation_happens_before_io(monkeypatch):
    monkeypatch.setattr("api.market_api.follow_up", lambda *a: pytest.fail("invalid date must not query"))
    monkeypatch.setattr("api.market_api.follow_up_chart", lambda *a: pytest.fail("invalid request must not query"))
    c = client()
    assert c.get("/api/v1/market/tracking?signal_date=bad").status_code == 422
    assert c.get("/api/v1/market/tracking/600000.SH?run_id=bad").status_code == 422
    assert c.get("/api/v1/market/tracking/bad?run_id=410395ca-57cc-4f10-bd62-4aeb023f223b").status_code == 422


def test_unavailable_history_returns_404_and_chart_is_pinned_to_run(monkeypatch):
    def missing(*args):
        raise LookupError("该日期没有已发布的历史筛选快照")

    monkeypatch.setattr("api.market_api.follow_up", missing)
    assert client().get("/api/v1/market/tracking?signal_date=2026-01-01").status_code == 404
    seen = []

    def chart(*args):
        seen.append(args)
        return {"data": []}

    monkeypatch.setattr("api.market_api.follow_up_chart", chart)
    run = "410395ca-57cc-4f10-bd62-4aeb023f223b"
    assert client().get(f"/api/v1/market/tracking/600000.SH?run_id={run}&timeframe=60F").status_code == 200
    assert seen == [("600000.SH", run, "60F")]


def test_static_episode_route_is_not_mistaken_for_a_stock_code(monkeypatch):
    seen = []
    monkeypatch.setattr("api.market_api.episode_tracking", lambda view: seen.append(view) or {"data": []})
    c = client()
    assert c.get("/api/v1/market/tracking/episodes?view=archive").status_code == 200
    assert seen == ["archive"]
    assert c.get("/api/v1/market/tracking/episodes?view=bad").status_code == 422
