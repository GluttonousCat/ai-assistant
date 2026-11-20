from fastapi import FastAPI
from fastapi.testclient import TestClient

from api import auth_api, deps
from api.middleware import AuthMiddleware
from core.security import TokenPayload


def test_super_admin_requires_exact_active_account(monkeypatch):
    monkeypatch.delenv("SUPER_ADMIN", raising=False)
    assert not auth_api._is_super_admin("SampleAdmin", "admin")
    monkeypatch.setenv("SUPER_ADMIN", "SampleAdmin")
    assert auth_api._is_super_admin("SampleAdmin", "admin")
    assert not auth_api._is_super_admin("Sampleadmin", "admin")
    assert not auth_api._is_super_admin("SampleAdmin", "user")
    monkeypatch.setenv("SUPER_ADMIN", "OtherAdmin")
    assert auth_api._is_super_admin("OtherAdmin", "admin")
    assert not auth_api._is_super_admin("SampleAdmin", "admin")


def test_live_account_and_role_checked_once_per_request(monkeypatch):
    app = FastAPI()
    app.add_middleware(AuthMiddleware)
    app.include_router(auth_api.auth_router)
    db = {42: {"username": "SampleAdmin", "role": "admin"}}
    reads = []

    def lookup(uid):
        reads.append(uid)
        return db.get(uid)

    monkeypatch.setenv("SUPER_ADMIN", "SampleAdmin")
    monkeypatch.setattr(deps, "active_role", lookup)
    monkeypatch.setattr(deps, "decode_token", lambda _: TokenPayload(42, "SampleAdmin", "user", 0))
    monkeypatch.setattr(auth_api.accounts, "list_users", lambda: [])
    client = TestClient(app)
    headers = {"Authorization": "Bearer test-session"}
    assert client.get("/api/auth/users", headers=headers).status_code == 200
    assert reads == [42]
    db[42]["role"] = "user"
    assert client.get("/api/auth/users", headers=headers).status_code == 403
    db.clear()
    assert client.get("/api/auth/users", headers=headers).status_code == 401
