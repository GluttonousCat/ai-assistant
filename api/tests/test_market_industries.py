import asyncio
import threading

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import market_api as api
from api.middleware import AuthMiddleware
from skills.trend import intraday
from tools.market import queries


def test_industry_metadata_is_current_level_two_and_does_not_fill_missing_with_level_one(monkeypatch):
    monkeypatch.setattr(queries, "_stock_industries", lambda: [
        {"ts_code": "000959.SZ", "industry_l2": "普钢"},
        {"ts_code": "600001.SH", "industry_l2": None},
    ])
    result = queries.stock_industries()
    assert result["data"] == {"000959.SZ": "普钢", "600001.SH": None}
    assert result["scope"] == "current" and result["classification"] == "申万二级行业"


def test_industry_api_runs_query_off_event_loop_and_requires_auth(monkeypatch):
    caller = threading.get_ident()
    def query():
        assert threading.get_ident() != caller
        return {"data": {"000959.SZ": "普钢"}}
    monkeypatch.setattr(api, "stock_industries", query)
    assert asyncio.run(api.market_industries())["data"]["000959.SZ"] == "普钢"
    app = FastAPI()
    app.include_router(api.market_router)
    app.add_middleware(AuthMiddleware)
    assert TestClient(app).get("/api/v1/market/industries").status_code == 401


def test_existing_intraday_archive_is_ranked_without_mutating_saved_report(monkeypatch):
    report = {"data": [
        {"ts_code": "600002.SH", "score": 65, "new_today": True},
        {"ts_code": "600001.SH", "score": 78, "new_today": False},
    ]}
    monkeypatch.setattr(intraday, "intraday_report", lambda: report)
    monkeypatch.setattr(intraday, "intraday_schedule", lambda: {})
    assert intraday.dashboard()["data"][0]["ts_code"] == "600001.SH"
    assert report["data"][0]["ts_code"] == "600002.SH"
