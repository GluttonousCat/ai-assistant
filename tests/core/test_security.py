from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

from core import security


def test_fallback_signing_secret_is_shared_across_threads_and_token_verification(
    monkeypatch,
):
    monkeypatch.setattr(security, "_fallback_secret", None)
    monkeypatch.setattr(
        security,
        "get_config",
        lambda: SimpleNamespace(env=lambda *args: "", get=lambda key, default: default),
    )
    with ThreadPoolExecutor(8) as pool:
        secrets = list(pool.map(lambda _: security._secret(), range(32)))
    assert len(set(secrets)) == 1
    token = security.create_token(7, "example", "user")
    assert security.decode_token(token).user_id == 7


def test_configured_signing_secret_takes_priority(monkeypatch):
    monkeypatch.setattr(security, "_fallback_secret", "temporary")
    monkeypatch.setattr(
        security, "get_config", lambda: SimpleNamespace(env=lambda *args: "configured")
    )
    assert security._secret() == "configured"
