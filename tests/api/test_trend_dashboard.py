from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from api.market_api import market_router
from api.middleware import AuthMiddleware


def client():
    app = FastAPI()
    app.include_router(market_router)
    return TestClient(app)


def test_no_snapshot_returns_actionable_unavailable_without_ddl(monkeypatch):
    monkeypatch.setattr("api.market_api.dashboard", lambda **kw: None)
    response = client().get("/api/v1/market/dashboard")
    assert response.status_code == 503
    assert "python -m workflows.market.scan_trend" in response.json()["detail"]


def test_dashboard_filter_is_optional_and_validated_before_read(monkeypatch):
    floors = []

    def dashboard(*, min_numeric_score):
        floors.append(min_numeric_score)
        return {"metadata": {"total": 3}, "data": []}

    monkeypatch.setattr("api.market_api.dashboard", dashboard)
    c = client()
    path = "/api/v1/market/dashboard"
    assert c.get(path).status_code == 200
    assert c.get(path, params={"min_numeric_score": 72}).json()["metadata"] == {"total": 3}
    assert c.get(path, params={"min_numeric_score": -1}).status_code == 422
    assert floors == [None, 72]


def test_stock_chart_is_pinned_to_requested_snapshot(monkeypatch):
    run_id = "410395ca-57cc-4f10-bd62-4aeb023f223b"
    seen = []

    def context(*args):
        seen.append(args)
        return {
            "as_of": "2026-11-11 15:00:00",
            "metadata": {"start": "2026-01-01", "config": {"fast": 20}},
        }

    def detail(*args):
        seen.append(args)
        return {"data": [], "code": args[0]}

    monkeypatch.setattr("api.market_api.dashboard", context)
    monkeypatch.setattr("api.market_api.stock_detail", detail)
    response = client().get(f"/api/v1/market/dashboard/600000.SH?run_id={run_id}")
    assert response.status_code == 200
    assert seen[0] == (run_id, False)
    assert seen[1][2] == "2026-11-11 15:00:00"


@pytest.mark.parametrize(
    "path", ["/api/v1/market/dashboard", "/api/v1/market/dashboard/600000.SH", "/api/v1/market/freshness"]
)
def test_global_auth_covers_dashboard(path):
    app = FastAPI()
    app.include_router(market_router)
    app.add_middleware(AuthMiddleware)
    assert TestClient(app).get(path).status_code == 401


def test_invalid_code_or_snapshot_is_rejected_before_access(monkeypatch):
    def fail(*args):
        raise AssertionError("not reached")

    monkeypatch.setattr("api.market_api.dashboard", fail)
    c = client()
    assert c.get("/api/v1/market/dashboard/bad").status_code == 422
    assert c.get("/api/v1/market/dashboard/600000.SH?run_id=bad").status_code == 422


def test_daily_chart_uses_its_own_completed_cutoff(monkeypatch):
    seen = []
    monkeypatch.setattr(
        "api.market_api.dashboard",
        lambda *a: {
            "as_of": "2026-11-13 15:00:00",
            "metadata": {
                "start": "2026-01-01",
                "config": {},
                "timeframes": {
                    "1D": {"as_of": "2026-11-13 15:00:00"},
                    "60F": {"as_of": "2026-11-12 15:00:00"},
                },
            },
        },
    )

    def detail(*args):
        seen.append(args)
        return {"data": []}

    monkeypatch.setattr("api.market_api.stock_detail", detail)
    c = client()
    assert c.get("/api/v1/market/dashboard/600000.SH?timeframe=1D").status_code == 200
    assert seen[-1][2] == "2026-11-13 15:00:00" and seen[-1][-1] == "1D"
    assert c.get("/api/v1/market/dashboard/600000.SH?timeframe=60F").status_code == 200
    assert seen[-1][2] == "2026-11-12 15:00:00"
    assert c.get("/api/v1/market/dashboard/600000.SH?timeframe=5F").status_code == 422


def test_legacy_snapshot_does_not_invent_daily_results(monkeypatch):
    monkeypatch.setattr("api.market_api.dashboard", lambda *a: {"metadata": {}})
    assert (
        client().get("/api/v1/market/dashboard/600000.SH?timeframe=1D").status_code
        == 404
    )
