from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from tools.auth import users


def test_existing_user_table_never_recreates_seed_admin(monkeypatch):
    monkeypatch.setattr(users, "ensure_auth_tables", lambda: None)
    monkeypatch.setattr(users.auth_store, "user_count", lambda: 1)
    monkeypatch.setattr(
        users, "get_config", lambda: SimpleNamespace(env=lambda key, default: default)
    )
    create = MagicMock()
    monkeypatch.setattr(users, "create_user", create)
    users.ensure_auth_schema()
    create.assert_not_called()


def test_creation_enforces_registration_password_length_before_persistence(monkeypatch):
    insert = MagicMock()
    monkeypatch.setattr(users.auth_store, "insert_user", insert)
    with pytest.raises(ValueError, match="8"):
        users.create_user("example", "1234567")
    insert.assert_not_called()


def test_inactive_users_are_rejected_before_password_verification(monkeypatch):
    monkeypatch.setattr(users, "get_user", lambda username: {"is_active": False})
    verify = MagicMock()
    monkeypatch.setattr(users, "verify_password", verify)
    assert users.authenticate("example", "password") is None
    verify.assert_not_called()
