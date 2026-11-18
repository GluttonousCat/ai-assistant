from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from api import monitor_api as monitor
from api.deps import require_admin


def client():
    app = FastAPI()
    app.include_router(monitor.monitor_router)
    return app, TestClient(app)


def test_monitor_routes_require_admin_before_reading_logs(monkeypatch):
    app, http = client()
    calls = []
    monkeypatch.setattr(monitor, "read_logs", lambda **kw: calls.append(kw))
    assert http.get("/api/monitor/logs").status_code == 401

    def denied():
        raise HTTPException(403, "仅管理员")

    app.dependency_overrides[require_admin] = denied
    assert http.get("/api/monitor/logs").status_code == 403
    assert http.get("/api/monitor/state").status_code == 403
    assert http.post("/api/monitor/stream").status_code == 403
    assert not calls


def test_logs_validation_and_local_snapshot(monkeypatch):
    app, http = client()
    app.dependency_overrides[require_admin] = lambda: None
    monkeypatch.setattr(monitor, "read_logs", lambda **kw: {"lines": [], **kw})
    assert http.get("/api/monitor/logs?limit=1001").status_code == 422
    assert (
        http.get("/api/monitor/logs?source=tools&level=ERROR").json()["source"]
        == "tools"
    )
    monkeypatch.setattr(monitor, "_pg_state", lambda: {"ok": False, "pending": True})
    monkeypatch.setattr(
        monitor,
        "background_snapshot",
        lambda: {"active": [{"id": "live"}], "history": []},
    )
    result = http.get("/api/monitor/state").json()
    assert result["background"]["active"][0]["id"] == "live"
    assert result["pg"]["pending"]
