import importlib
import subprocess
import sys

import pytest


@pytest.mark.parametrize(("old_name", "current_name"), [
    ("jobs.intraday_scheduler", "workflows.market.intraday_scheduler"),
    ("jobs.lifecycle", "workflows.runtime.lifecycle"),
    ("jobs.quant_worker", "workflows.quant.worker"),
    ("jobs.check_boundaries", "workflows.diagnostics.check_boundaries"),
    ("jobs.backtest_mtf", "workflows.quant.backtests.mtf"),
    ("jobs.cache_mainboard_minutes", "workflows.quant.inputs.cache_mainboard_minutes"),
    ("jobs.research_quality_60f", "workflows.quant.studies.quality_60f"),
])
def test_legacy_import_uses_same_module(old_name, current_name):
    assert importlib.import_module(old_name) is importlib.import_module(current_name)


@pytest.mark.parametrize(("command", "options"), [
    (["-m", "jobs.refresh_market"], [b"--skip-quotes", b"--end"]),
    (["-m", "workflows.market.refresh"], [b"--skip-quotes", b"--end"]),
    (["-m", "jobs.research_quality_60f"], [b"--declare-only"]),
    (["-m", "jobs.backtest_reversal_exits"], [b"--publish-only", b"--offline"]),
    (["-m", "jobs.cache_mainboard_minutes"], [b"--workers"]),
    (["jobs/smoke_mcp_stdio.py"], [b"--args-json"]),
])
def test_command_help_preserves_options(command, options, pytestconfig):
    result = subprocess.run(
        [sys.executable, *command, "--help"],
        cwd=pytestconfig.rootpath,
        capture_output=True,
        timeout=30,
        check=True,
    )
    for option in options:
        assert option in result.stdout
