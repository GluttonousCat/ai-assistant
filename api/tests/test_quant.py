from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from api.quant_api import quant_router
from api.middleware import AuthMiddleware


def client(auth=False):
    app = FastAPI()
    app.include_router(quant_router)
    if auth:
        app.add_middleware(AuthMiddleware)
    return TestClient(app)


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/quant/strategies",
        "/api/v1/quant/backtests",
        "/api/v1/quant/backtests/410395ca-57cc-4f10-bd62-4aeb023f223b",
    ],
)
def test_all_quant_endpoints_require_login(path):
    assert client(True).get(path).status_code == 401


def test_missing_results_is_empty_without_request_time_ddl(monkeypatch):
    monkeypatch.setattr("api.quant_api.service.results", lambda limit: [])
    assert client().get("/api/v1/quant/backtests").json() == {"data": []}


def test_unknown_result_and_invalid_pagination(monkeypatch):
    monkeypatch.setattr("api.quant_api.service.result", lambda *a: None)
    c = client()
    assert c.get("/api/v1/quant/backtests/bad").status_code == 422
    assert (
        c.get(
            "/api/v1/quant/backtests/410395ca-57cc-4f10-bd62-4aeb023f223b"
        ).status_code
        == 404
    )
    assert c.get("/api/v1/quant/backtests?limit=1000").status_code == 422
    assert (
        c.get(
            "/api/v1/quant/backtests/410395ca-57cc-4f10-bd62-4aeb023f223b?offset=-1"
        ).status_code
        == 422
    )
    assert (
        c.get(
            "/api/v1/quant/backtests/410395ca-57cc-4f10-bd62-4aeb023f223b?limit=201"
        ).status_code
        == 422
    )


def test_registry_contains_both_opportunities_and_combined_strategy():
    rows = client().get("/api/v1/quant/strategies").json()["data"]
    assert len(rows) == 8
    reversal = next(r for r in rows if r["id"] == "mtf_reversal_v6")
    assert reversal["entry_config"]["fresh_reversal"] is True
    assert reversal["entry_config"]["reversal_cycle_reset"] is True
    assert reversal["timeframes"] == ["1D"] and reversal["trend_timeframe"] == "60F"
    assert all(
        not r["entry_config"].get("fresh_reversal", False)
        for r in rows
        if r["id"] not in ("mtf_reversal_v6", "mtf_risk_v7")
    )
    current = [r for r in rows if r.get("generation") == "v5"]
    assert len(current) == 3 and all(
        r["timeframes"] == ["1D"] and r["trend_timeframe"] == "60F" for r in current
    )
    assert {r["family"] for r in rows} == {None, "trend_pullback", "base_rebound"}


def test_detail_reads_only_requested_page(monkeypatch):
    seen = []

    def load(*args):
        seen.append(args)
        return {"trades": []}

    monkeypatch.setattr("api.quant_api.service.result", load)
    run = "410395ca-57cc-4f10-bd62-4aeb023f223b"
    assert (
        client().get(f"/api/v1/quant/backtests/{run}?offset=30&limit=30").status_code
        == 200
    )
    assert seen == [(run, 30, 30)]
