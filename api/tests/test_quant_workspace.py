from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from api.deps import get_current_user
from api.middleware import AuthMiddleware
from api.quant_api import quant_router
from skills.quant.workspace_models import StrategyDraft
from storage.repositories import quant_workspace as store


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setattr(store, "_root", lambda: tmp_path)
    app = FastAPI()
    app.include_router(quant_router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(user_id=1)
    return TestClient(app)


@pytest.mark.parametrize(
    "path,method",
    [
        ("", "get"),
        ("/availability", "get"),
        ("/drafts", "post"),
        ("/generate", "post"),
        ("/tasks", "post"),
        (f"/tasks/{uuid4()}/result", "get"),
    ],
)
def test_every_workspace_operation_is_authenticated(path, method):
    app = FastAPI()
    app.include_router(quant_router)
    app.add_middleware(AuthMiddleware)
    assert (
        getattr(TestClient(app), method)("/api/v1/quant/workspace" + path).status_code
        == 401
    )


def test_http_only_saves_and_enqueues_never_computes(client, monkeypatch):
    monkeypatch.setattr(
        "skills.quant.workspace_runner.run_task",
        lambda *args: pytest.fail("HTTP must not run simulations"),
    )
    draft = client.post(
        "/api/v1/quant/workspace/drafts", json=StrategyDraft().model_dump()
    ).json()
    payload = dict(
        draft_id=draft["id"],
        request_id=str(uuid4()),
        start="2026-01-01",
        end="2026-11-12",
        sample_limit=50,
    )
    submitted = client.post("/api/v1/quant/workspace/tasks", json=payload)
    assert submitted.status_code == 202
    key = submitted.json()["id"]
    assert (
        client.post("/api/v1/quant/workspace/tasks", json=payload).json()["id"] == key
    )
    assert (
        client.get(f"/api/v1/quant/workspace/tasks/{key}").json()["status"] == "queued"
    )
    assert client.get(f"/api/v1/quant/workspace/tasks/{key}/result").status_code == 404
    assert (
        client.get("/api/v1/quant/workspace").json()["drafts"][0]["id"] == draft["id"]
    )


def test_unknown_draft_foreign_owner_and_invalid_uuid(client):
    payload = dict(
        draft_id=str(uuid4()),
        request_id=str(uuid4()),
        start="2026-01-01",
        end="2026-11-12",
    )
    assert client.post("/api/v1/quant/workspace/tasks", json=payload).status_code == 404
    assert (
        client.post(
            "/api/v1/quant/workspace/tasks", json={**payload, "draft_id": "../../x"}
        ).status_code
        == 422
    )
    draft = store.save_draft(2, StrategyDraft().model_dump())
    assert (
        client.post(
            "/api/v1/quant/workspace/tasks", json={**payload, "draft_id": draft["id"]}
        ).status_code
        == 404
    )


def test_generation_errors_are_readable_and_do_not_leak_provider_text(
    client, monkeypatch
):
    def fail(request):
        raise RuntimeError("private provider response")

    monkeypatch.setattr("api.quant_api.workspace.generate", fail)
    response = client.post(
        "/api/v1/quant/workspace/generate", json={"text": "主板底部反转"}
    )
    assert response.status_code == 502
    assert "private" not in response.text
    assert (
        client.post(
            "/api/v1/quant/workspace/drafts", json={"entry": {"max_risk_pct": 10}}
        ).status_code
        == 422
    )
