"""HTTP boundary tests with no database, network or model calls."""
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.deps import get_current_user
from api.finance import fin_router
from core.security import TokenPayload


@pytest.fixture
def client(monkeypatch):
    from harness import loop
    seen = []

    async def fake_stream(text, session_id, sse):
        seen.append(session_id)
        yield sse("delta", {"text": "ok"})

    monkeypatch.setattr(loop, "run_agent_stream", fake_stream)
    app = FastAPI()
    app.include_router(fin_router)
    app.dependency_overrides[get_current_user] = lambda: TokenPayload(
        user_id=7, username="tester", role="user", exp=9999999999)
    return TestClient(app), seen, app


def test_session_id_is_bound_to_authenticated_user(client):
    http, seen, app = client
    body = {"text": "问题", "session_id": "same-client-id"}
    assert http.post("/api/v1/agent/stream", json=body).status_code == 200
    app.dependency_overrides[get_current_user] = lambda: TokenPayload(
        user_id=8, username="another", role="user", exp=9999999999)
    http.post("/api/v1/agent/stream", json=body)
    assert seen == ["user-7-same-client-id", "user-8-same-client-id"]


@pytest.mark.parametrize("body", [
    {"text": "   "}, {"text": 123}, {"text": "a" * 16001},
    {"text": "hi", "session_id": {"bad": "type"}},
    {"text": "hi", "session_id": "../path"},
])
def test_invalid_input_rejected_before_streaming(client, body):
    http, seen, _ = client
    assert http.post("/api/v1/agent/stream", json=body).status_code == 422
    assert not seen


def test_legacy_management_routes_require_authentication():
    from api.router import router
    app = FastAPI()
    app.include_router(router)
    http = TestClient(app)
    for method, path in [("get", "/accounts"), ("get", "/admin/scheduler-status"),
                         ("post", "/crawl/latest"), ("post", "/admin/sync-today")]:
        assert getattr(http, method)(path).status_code == 401
    assert http.get("/health").status_code == 200
