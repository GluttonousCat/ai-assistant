"""Resolve former jobs.* imports, module commands and supported script paths."""
from importlib import import_module
from importlib.abc import Loader
from importlib.util import spec_from_loader
from pathlib import Path
from runpy import run_module
import sys

# A direct jobs/<script>.py launch imports _compat from the script directory.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from workflows.quant.studies.provenance import STUDY_MODULES  # noqa: E402


_MOVED = {
    **{f"jobs.{old}": current for old, current in STUDY_MODULES.items()},
    "jobs.agent_trace_demo": "workflows.diagnostics.agent_trace",
    "jobs.annotate_trend_context": "workflows.market.annotate_trend_context",
    "jobs.backtest_mtf": "workflows.quant.backtests.mtf",
    "jobs.backtest_quant": "workflows.quant.backtests.quant",
    "jobs.backtest_reversal": "workflows.quant.backtests.reversal",
    "jobs.backtest_reversal_exits": "workflows.quant.backtests.reversal_exits",
    "jobs.cache_intraday_prefix": "workflows.market.collect_intraday",
    "jobs.cache_mainboard_minutes": "workflows.quant.inputs.cache_mainboard_minutes",
    "jobs.check_boundaries": "workflows.diagnostics.check_boundaries",
    "jobs.daily_digest": "workflows.reports.digest",
    "jobs.daily_fetch_zxsq": "workflows.reports.fetch",
    "jobs.fetch_minute_history": "workflows.market.fetch_minute_history",
    "jobs.health_sentinel": "workflows.runtime.health_sentinel",
    "jobs.intraday_scheduler": "workflows.market.intraday_scheduler",
    "jobs.lifecycle": "workflows.runtime.lifecycle",
    "jobs.market_digest_scheduler": "workflows.market.digest_scheduler",
    "jobs.merge_report_duplicates": "workflows.reports.deduplicate",
    "jobs.operations": "workflows.runtime.operations",
    "jobs.quant_worker": "workflows.quant.worker",
    "jobs.refresh_market": "workflows.market.refresh",
    "jobs.repair_trend_factors": "workflows.market.repair_factors",
    "jobs.scan_intraday_watch": "workflows.market.scan_intraday",
    "jobs.scan_trend": "workflows.market.scan_trend",
    "jobs.scheduled_task": "workflows.runtime.scheduled_task",
    "jobs.scheduler": "workflows.market.daily_scheduler",
    "jobs.send_intraday_digest": "workflows.market.send_midday_digest",
    "jobs.send_market_digest": "workflows.market.send_close_digest",
    "jobs.smoke_mcp_stdio": "workflows.diagnostics.smoke_mcp_stdio",
    "jobs.smoke_mcp_tools": "workflows.diagnostics.smoke_mcp_tools",
    "jobs.sync_backtest_inputs": "workflows.quant.inputs.sync_backtest_inputs",
    "jobs.sync_intraday": "workflows.market.sync_intraday",
    "jobs.sync_remaining_market": "workflows.market.sync_remaining",
    "jobs.sync_zxsq_to_pg": "workflows.reports.sync",
    "jobs.wecom_bot": "workflows.runtime.wecom_bot",
    "jobs.zsxq_scheduler": "workflows.reports.scheduler",
}


def forward(legacy_name, execution_name):
    """Support the historical module name and explicitly supported script paths."""
    destination = _MOVED[legacy_name]
    if execution_name == "__main__":
        run_module(destination, run_name="__main__", alter_sys=True)
    else:
        sys.modules[execution_name] = import_module(destination)


class _WorkflowAlias(Loader):
    def exec_module(self, module):
        # Both names must share globals, including scheduler and worker state.
        forward(module.__name__, module.__name__)

    def get_code(self, fullname):
        # runpy asks for code directly for "python -m jobs.<old name>". Run
        # the real module as __main__ with its own package for relative imports.
        return compile(
            f"from jobs._compat import forward\nforward({fullname!r}, __name__)",
            f"<{fullname}>",
            "exec",
        )


class WorkflowAliases:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname in _MOVED:
            return spec_from_loader(fullname, _WorkflowAlias())
        return None
