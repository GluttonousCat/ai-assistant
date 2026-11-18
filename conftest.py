"""Keep test observations out of the running application's task history."""

from types import SimpleNamespace

import pytest


@pytest.fixture(autouse=True)
def isolate_task_observations(tmp_path, monkeypatch):
    from core import observability

    monkeypatch.setattr(
        observability, "get_config", lambda: SimpleNamespace(root_dir=tmp_path)
    )
