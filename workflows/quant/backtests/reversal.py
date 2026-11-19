"""Frozen-exit comparison for the new 60F reversal gate on the existing pool.

No exit/threshold search. Same 2026 index pool and structure_2r execution as v5.1;
current expanded pool is excluded from this comparison to avoid mixing inputs.
"""

from dataclasses import asdict
import json
from pathlib import Path
from uuid import uuid4

import pandas as pd

from core.config import get_config
from workflows.quant.backtests.mtf import prepare, window_series, WINDOWS, PROFILES, START, END
from skills.quant.engine import simulate
from skills.quant.report import write_result
from skills.trend.mtf import FRESH_VERSION, MtfConfig
from storage.repositories.backtest import input_dates, publish
from storage.schema.quant import ensure_quant_schema


def main():
    cfg = get_config()
    folder = Path(cfg.root_dir) / "output/quant/reversal_v6_1"
    entry = MtfConfig(fresh_reversal=True, reversal_cycle_reset=True)
    payload = prepare(folder, workers=4, entry_cfg=entry)
    days, limits, _ = input_dates(START, END)
    if set(days) - limits:
        raise ValueError("缺少真实限价日期")
    report = {}
    for window, (start, end) in WINDOWS.items():
        series = window_series(payload["series"], start, end)
        timeline = pd.DatetimeIndex(
            [
                pd.Timestamp(day) + pd.Timedelta(hours=15)
                for day in days
                if start <= str(day) <= end
            ]
        )
        r = simulate(
            series,
            timeline,
            "mtf_reversal_v6",
            "1D",
            "fixed_snapshot",
            PROFILES["structure_2r"],
        )
        r.update(
            run_id=str(uuid4()),
            suite_id=payload["declaration"]["suite_id"],
            strategy_id="mtf_reversal_v6",
            strategy_version=FRESH_VERSION,
            strategy_config=asdict(entry),
            timeframe="1D",
            universe_mode="fixed_snapshot",
            start=start,
            end=end,
            diagnostics={
                "trend_timeframe": "60F",
                "entry_timeframe": "1D",
                "exit_profile": "structure_2r",
                "in_sample": True,
                "research_window": window,
                "universe_size": len(payload["members"]),
                "usable_stocks": len(series),
                "bar_count": sum(len(s.times) for s in series),
                "exit_selection": "固定沿用v5.1结构止损+2R，不按本轮各段结果重新选参数",
                "warnings": [
                    "当前指数成分固定回放，有幸存者/成分前视偏差",
                    "已参考2026样本；非独立样本外验证",
                    "新扩充股票不混入本次同池对照",
                ],
            },
        )
        write_result(folder / window, r)
        report[window] = r["metrics"]
        if window == "full":
            ensure_quant_schema()
            publish(r)
    (folder / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
