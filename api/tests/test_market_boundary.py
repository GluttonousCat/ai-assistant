from decimal import Decimal
import pandas as pd
from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.market_api import market_router


def test_market_response_preserves_decimal_and_uses_repository_path(monkeypatch):
    seen = []

    def query(*args):
        seen.append(args)
        return pd.DataFrame([{"close": Decimal("10.123456"), "bar_end": pd.Timestamp("2026-11-11 15:00") }])

    monkeypatch.setattr("api.market_api.query_intraday", query)
    app = FastAPI()
    app.include_router(market_router)
    response = TestClient(app).get("/api/v1/market/bars?code=600000.SH")
    assert response.status_code == 200
    assert response.json()["data"][0]["close"] == "10.123456"
    assert seen


def test_market_rejects_invalid_period_before_access(monkeypatch):
    def forbidden(*args):
        raise AssertionError("must not access storage")
    monkeypatch.setattr("api.market_api.query_intraday", forbidden)
    app = FastAPI()
    app.include_router(market_router)
    assert TestClient(app).get("/api/v1/market/bars?code=600000.SH&period=7").status_code == 422


def test_market_is_covered_by_global_auth_middleware():
    from api.middleware import AuthMiddleware
    app = FastAPI()
    app.include_router(market_router)
    app.add_middleware(AuthMiddleware)
    assert TestClient(app).get("/api/v1/market/bars?code=920001.BJ").status_code == 401
