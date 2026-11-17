from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from api.deps import require_admin
from api.router import router


def application(operations=None):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_admin] = lambda: object()
    if operations is not None:
        app.state.operations = operations
    return TestClient(app)


def test_admin_routes_use_injected_operations_without_owning_scheduler_state():
    operations = SimpleNamespace(
        trigger_sync=AsyncMock(return_value={"status": "queued"}),
        scheduler_status=MagicMock(return_value={"running": True, "last_result": None}),
    )
    http = application(operations)
    assert http.post("/admin/sync-today").json() == {
        "status": "queued",
        "result": {"status": "queued"},
    }
    assert http.get("/admin/scheduler-status").json() == {
        "running": True,
        "last_result": None,
    }
    operations.trigger_sync.assert_awaited_once()
    operations.scheduler_status.assert_called_once()


def test_missing_lifecycle_reports_unavailable_instead_of_starting_hidden_jobs():
    http = application()
    response = http.get("/admin/scheduler-status")
    assert (
        response.status_code == 503
        and response.json()["detail"] == "后台服务尚未初始化"
    )


def test_report_sync_preserves_background_queue_and_resolved_group():
    operations = SimpleNamespace(sync_reports=MagicMock())
    response = application(operations).post("/crawl/sync-reports?group_id=group")
    assert response.status_code == 200 and response.json()["status"] == "started"
    operations.sync_reports.assert_called_once_with("group")
