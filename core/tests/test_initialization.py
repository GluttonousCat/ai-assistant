from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
import threading

import pytest

from core import config as m
from core import logger as log


def isolated_config(monkeypatch, root):
    class IsolatedConfig(m.Config):
        _instance = None
        _lock = threading.Lock()

    monkeypatch.setattr(m, "_find_project_root", lambda: root)
    monkeypatch.setattr(m, "_load_dotenv", lambda _: None)
    return IsolatedConfig


def test_concurrent_config_users_wait_for_complete_initialization(
    tmp_path, monkeypatch
):
    cfg = isolated_config(monkeypatch, tmp_path)
    (tmp_path / "config.yaml").write_text("app: {name: ready}", encoding="utf-8")
    entered, release = threading.Event(), threading.Event()

    def slow_load(_):
        entered.set()
        assert release.wait(2)

    monkeypatch.setattr(m, "_load_dotenv", slow_load)
    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(cfg)
        assert entered.wait(1)
        second_started = threading.Event()

        def second_reader():
            second_started.set()
            return cfg()

        second = pool.submit(second_reader)
        try:
            assert second_started.wait(1)
            with pytest.raises(FutureTimeoutError):
                second.result(timeout=0.05)
        finally:
            release.set()
        assert first.result() is second.result()
        assert second.result().get("app.name") == "ready"


@pytest.mark.parametrize("invalid", ["- invalid", "[", "[]"])
def test_failed_config_load_can_be_retried(tmp_path, monkeypatch, invalid):
    cfg = isolated_config(monkeypatch, tmp_path)
    path = tmp_path / "config.yaml"
    path.write_text(invalid, encoding="utf-8")
    with pytest.raises((ValueError, m.yaml.YAMLError)):
        cfg()
    path.write_text("app: {name: recovered}", encoding="utf-8")
    assert cfg().get("app.name") == "recovered"


def test_concurrent_log_setup_installs_one_pair_of_handlers(tmp_path, monkeypatch):
    monkeypatch.setattr(log, "_project_root", lambda: tmp_path)
    monkeypatch.setattr(
        log, "_settings", lambda: {"level": "INFO", "dir": "logs", "backup_count": 1}
    )
    bucket = "refactor_test_logger"
    try:
        with ThreadPoolExecutor(12) as pool:
            children = list(
                pool.map(lambda i: log.get_logger(f"{bucket}.worker{i}"), range(48))
            )
        parent = log._LogManager._buckets[bucket]
        assert len(parent.handlers) == 2
        assert len({id(child.parent) for child in children}) == 1
    finally:
        parent = log._LogManager._buckets.pop(bucket, None)
        if parent:
            for handler in parent.handlers[:]:
                parent.removeHandler(handler)
                handler.close()
