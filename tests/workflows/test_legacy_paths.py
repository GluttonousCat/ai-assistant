import importlib
import subprocess
import sys

import pytest


@pytest.mark.parametrize(("old_name", "current_name"), [
    ("jobs.intraday_scheduler", "workflows.market.intraday_scheduler"),
    ("jobs.lifecycle", "workflows.runtime.lifecycle"),
    ("jobs.quant_worker", "workflows.quant.worker"),
])
def test_legacy_import_uses_same_module_and_scheduler_state(old_name, current_name):
    assert importlib.import_module(old_name) is importlib.import_module(current_name)


@pytest.mark.parametrize("module", ["jobs.refresh_market", "workflows.market.refresh"])
def test_market_refresh_module_remains_a_cli(module, pytestconfig):
    result = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        cwd=pytestconfig.rootpath,
        capture_output=True,
        timeout=30,
        check=True,
    )
    assert b"--skip-quotes" in result.stdout and b"--end" in result.stdout
