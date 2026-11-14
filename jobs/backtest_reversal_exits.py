"""Compare predeclared exits on frozen v6.1 entries, without entry retuning."""

from dataclasses import asdict, replace
import argparse
import json
from pathlib import Path
import pickle
from uuid import uuid4

import pandas as pd

from core.config import get_config
from jobs.backtest_quant import _database_unit
from jobs.backtest_mtf import PROFILES, WINDOWS, window_series, START, END
from skills.quant.engine import simulate
from skills.quant.report import write_result
from storage.repositories.backtest import input_dates, publish
from storage.schema.quant import ensure_quant_schema


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--offline",
        action="store_true",
        help="使用冻结报告交易日历与缓存真实限价，不访问或写入PG",
    )
    parser.add_argument(
        "--publish-only",
        action="store_true",
        help="只发布已完成的离线full报告；按run_id幂等重试",
    )
    args = parser.parse_args()
    root = Path(get_config().root_dir)
    if args.publish_only:
        if args.offline:
            parser.error("--offline与--publish-only不可并用")
        for name in ["structure_2r", "reversal_staged"]:
            path = (
                root
                / "output/quant/reversal_exits_v2/full"
                / name
                / "1D_mtf_reversal_v6_fixed_snapshot.json"
            )
            result = json.loads(path.read_text(encoding="utf-8"))
            _database_unit(lambda: publish(result))
        print("Published two completed exit reports")
        return
    source = root / "output/quant/reversal_v6_1/prepared.pkl"
    with source.open("rb") as stream:
        payload = pickle.load(stream)  # Only the local, project-generated cache.
    if (
        payload["declaration"]["calculation_version"]
        != "60f-early-reversal-daily-entry-v6.1"
    ):
        raise ValueError("Frozen entry cache version mismatch")
    folder = root / "output/quant/reversal_exits_v2"
    folder.mkdir(parents=True, exist_ok=True)
    profiles = {
        "structure_2r": PROFILES["structure_2r"],
        "reversal_staged": replace(
            PROFILES["structure_2r"],
            exit_policy="reversal",
            trend_exit=True,
            protect_after_r=1,
            trail_after_r=2,
        ),
    }
    suite = str(uuid4())
    (folder / "declaration.json").write_text(
        json.dumps(
            dict(
                source=str(source.relative_to(root)),
                source_declaration=payload["declaration"],
                profiles={k: asdict(v) for k, v in profiles.items()},
                windows=WINDOWS,
                selection="No winner selection or threshold search; fixed 2026 membership and known entry examples are in-sample.",
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    if args.offline:
        baseline = json.loads(
            (
                root
                / "output/quant/reversal_v6_1/full/1D_mtf_reversal_v6_fixed_snapshot.json"
            ).read_text(encoding="utf-8")
        )
        if baseline["strategy_config"] != payload["declaration"]["strategy_config"]:
            raise ValueError("Frozen baseline does not match entry cache")
        days = [pd.Timestamp(p["date"]).date() for p in baseline["equity"]]
    else:
        days, limits, _ = _database_unit(lambda: input_dates(START, END))
        if set(days) - limits:
            raise ValueError("Missing actual daily price limits")
        _database_unit(ensure_quant_schema)
    report = {}
    for window, (start, end) in WINDOWS.items():
        series = window_series(payload["series"], start, end)
        timeline = pd.DatetimeIndex(
            [
                pd.Timestamp(d) + pd.Timedelta(hours=15)
                for d in days
                if start <= str(d) <= end
            ]
        )
        report[window] = {}
        for name, cfg in profiles.items():
            r = simulate(
                series, timeline, "mtf_reversal_v6", "1D", "fixed_snapshot", cfg
            )
            r.update(
                run_id=str(uuid4()),
                suite_id=suite,
                strategy_id="mtf_reversal_v6",
                strategy_version=payload["declaration"]["calculation_version"],
                strategy_config=payload["declaration"]["strategy_config"],
                timeframe="1D",
                universe_mode="fixed_snapshot",
                start=start,
                end=end,
                diagnostics=dict(
                    exit_profile=name,
                    research_window=window,
                    trend_timeframe="60F",
                    entry_timeframe="1D",
                    universe_size=len(payload["members"]),
                    usable_stocks=len(series),
                    bar_count=sum(len(s.times) for s in series),
                    in_sample=True,
                    exit_selection="两套预先固定离场规则对照，未按2026收益重新选参",
                    warnings=[
                        "固定当期名单回放，存在幸存者/成分前视偏差",
                        "已知2026形态参与设计，非独立样本外验证",
                        "原池冻结入场，不混入本轮首次回调扩展",
                        "未附加基准/超额字段",
                    ],
                ),
            )
            assert (
                abs(
                    r["metrics"]["ending_equity"]
                    - (
                        cfg.initial_cash
                        + sum(t["pnl"] for t in r["trades"])
                        + sum(t["unrealized_pnl"] for t in r["open_positions"])
                    )
                )
                < 1e-5
            )
            assert all(
                pd.Timestamp(t["entry_time"]).date()
                > pd.Timestamp(t["signal_time"]).date()
                and pd.Timestamp(t["exit_time"]).date()
                > pd.Timestamp(t["entry_time"]).date()
                for t in r["trades"]
            )
            write_result(folder / window / name, r)
            report[window][name] = r["metrics"]
            if window == "full" and not args.offline:
                _database_unit(lambda: publish(r))
    (folder / "comparison.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False))


if __name__ == "__main__":
    main()
