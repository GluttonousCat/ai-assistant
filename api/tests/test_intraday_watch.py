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


@pytest.mark.parametrize("path", ["/intraday", "/intraday/600000.SH?run_id=2026-11-17_1130"])
def test_intraday_reports_and_charts_require_auth(path):
    assert client(True).get("/api/v1/market" + path).status_code == 401


def test_no_report_does_not_fabricate_an_intraday_recommendation(monkeypatch):
    monkeypatch.setattr("api.market_api.intraday_dashboard", lambda: None)
    response = client().get("/api/v1/market/intraday")
    assert response.status_code == 200 and response.json()["as_of"] is None
    assert response.json()["data"] == []


def test_chart_parameters_and_report_identity_are_checked(monkeypatch):
    seen = []
    monkeypatch.setattr("api.market_api.intraday_chart", lambda *a: seen.append(a) or {"data": []})
    c = client()
    assert c.get("/api/v1/market/intraday/bad?run_id=2026-11-17_1130").status_code == 422
    assert c.get("/api/v1/market/intraday/600000.SH?run_id=../bad").status_code == 422
    assert c.get("/api/v1/market/intraday/600000.SH?run_id=2026-11-17_1200").status_code == 422
    assert c.get("/api/v1/market/intraday/600000.SH?run_id=2026-11-17_1130&timeframe=5F").status_code == 422
    assert not seen
    assert c.get("/api/v1/market/intraday/600000.SH?run_id=2026-11-17_1130").status_code == 200
    assert c.get("/api/v1/market/intraday/600000.SH?run_id=2026-11-17_1500&timeframe=1D").status_code == 200
    assert seen == [("600000.SH", "2026-11-17_1130", "60F"), ("600000.SH", "2026-11-17_1500", "1D")]
