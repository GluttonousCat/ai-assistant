"""Rank a collected closed prefix; official EOD scans and email state stay intact."""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from html import escape
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo

import pandas as pd

from core.config import get_config
from core.local_state import read_json, write_json, file_lock
from core.logger import get_logger
from core.observability import observed, task_progress
from workflows.market.collect_intraday import SLOTS
from skills.trend.intraday import observe, rank_candidates
from skills.trend.intraday_followup import build_followup
from skills.trend.chart_history import stock_chart
from skills.trend.digest import export_csv
from skills.trend.mtf import MtfConfig, minute_context
from skills.trend.parent_context import annotate_chart
from storage.repositories.intraday_watch import publish_report
from tools.market.chart_image import render_pair
from tools.market.trend_data import load_dashboard, scan_inputs, scan_daily_inputs, adjust_prices

logger = get_logger(__name__)


def batch(members, start, cutoff, daily_cutoff, config):
    codes = [m["ts_code"] for m in members]
    minute = scan_inputs(codes, start, cutoff)
    daily = scan_daily_inputs(codes, start, daily_cutoff)
    mg = dict(tuple(minute.groupby("ts_code"))) if not minute.empty else {}
    dg = dict(tuple(daily.groupby("ts_code"))) if not daily.empty else {}
    results = []
    for member in members:
        result = observe(mg.get(member["ts_code"], pd.DataFrame()), dg.get(member["ts_code"], pd.DataFrame()), cutoff, daily_cutoff, config)
        results.append({**member, **result})
    return results


def make_chart(row, snapshot, cutoff, daily_cutoff, folder):
    code, config = row["ts_code"], snapshot["metadata"]["config"]
    raw = scan_inputs([code], snapshot["metadata"]["start"], cutoff)
    minute, issue = adjust_prices(raw)
    daily = stock_chart(code, snapshot["metadata"]["start"], daily_cutoff, config, "1D")
    if issue or daily.get("issue"):
        raise ValueError("盘中图表所需真实行情或因子不完整")
    previous_factor = float(raw.loc[pd.to_datetime(raw.bar_end) <= pd.Timestamp(daily_cutoff), "adj_factor"].iloc[-1])
    current_factor = float(raw.adj_factor.iloc[-1])
    scale = previous_factor / current_factor
    for candle in daily["data"]:
        for field in ("open", "high", "low", "close", "ma8", "ma13", "ma21"):
            if candle.get(field) is not None:
                candle[field] *= scale
    native, di = adjust_prices(scan_daily_inputs([code], snapshot["metadata"]["start"], daily_cutoff))
    if di:
        raise ValueError(di)
    native[["open", "high", "low", "close"]] *= scale
    m = annotate_chart(minute_context(minute, MtfConfig(**config)), native)
    m["eligible"] = False
    detail = {"data": json.loads(m.to_json(orient="records", date_format="iso"))}
    png = render_pair(daily, detail, stop=row["stop_price"])
    (folder / "charts").mkdir(exist_ok=True)
    target = folder / "charts" / (code + ".png")
    temporary = target.with_suffix(".png.tmp")
    temporary.write_bytes(png)
    temporary.replace(target)


@observed("intraday_scan", "盘中趋势扫描")
def run(day, clock="11:30", wait=False, render_static=False):
    if clock not in SLOTS:
        raise ValueError("不支持的60F截止时点")
    cfg = get_config()
    task_progress(f"准备扫描 {day} {clock} 的60F趋势")
    folder = Path(cfg.root_dir) / "output" / "market" / "intraday_watch" / f"{day}_{clock.replace(':', '')}"
    if wait:
        deadline = time.monotonic() + 1800
        while not (folder / "audit.json").is_file():
            if time.monotonic() >= deadline:
                raise TimeoutError("等待盘中采集超过30分钟，请先检查采集进度")
            time.sleep(10)
    with file_lock(folder / "analysis.lock") as acquired:
        if not acquired:
            raise RuntimeError("已有同批盘中扫描在运行")
        audit = read_json(folder / "audit.json", strict=True)
        manifest = read_json(folder / "collection.json", strict=True)
        if not audit or audit["complete"] < .95 * audit["total"]:
            raise ValueError("今日60F采集覆盖不足95%，不发布看似完整的推荐")
        snapshot = load_dashboard(manifest["snapshot_run_id"], False)
        daily_cutoff = snapshot["metadata"].get("timeframes", {}).get("1D", {}).get("as_of", snapshot["as_of"])
        cutoff = f"{day} {clock}:00"
        if pd.Timestamp(daily_cutoff) >= pd.Timestamp(day):
            raise ValueError("盘中母趋势只能沿用之前完整日K")
        bad = {r["ts_code"] for r in audit["failed"]}
        members = [m for m in manifest["universe"] if m["ts_code"] not in bad]
        results = []
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(batch, members[i:i+50], snapshot["metadata"]["start"], cutoff, daily_cutoff, snapshot["metadata"]["config"]) for i in range(0, len(members), 50)]
            for future in as_completed(futures):
                results.extend(future.result())
                write_json(folder / "analysis_progress.json", {"completed": len(results), "total": len(members)})
                task_progress(completed=len(results), total=len(members))
                if len(results) % 250 == 0 or len(results) == len(members):
                    logger.info("盘中扫描进度 %s/%s", len(results), len(members))
        candidates = rank_candidates([r for r in results if r["status"] == "candidate"])
        report = {"run_id": folder.name, "as_of": cutoff, "daily_as_of": daily_cutoff,
            "start": snapshot["metadata"]["start"],
            "created_at": datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(), "source": "akshare-sina",
            "source_snapshot_run_id": snapshot["run_id"], "config": snapshot["metadata"]["config"],
            "strategy_version": snapshot.get("version"),
            "score_scope": "盘中观察最高80分；未计完整日K确认20分，不等同盘后100分评分",
            "universe": audit["total"], "collected": audit["complete"], "collection_failed": len(bad),
            "statuses": dict(Counter(r["status"] for r in results)), "data": candidates,
            "slots": [slot for slot in SLOTS if slot <= clock],
            "note": f"仅使用今天截至{clock}已收盘60F；日K背景截至上一完整日。eligible均为false，等待盘后复核。"}
        report["scan_status"] = {**dict.fromkeys(bad, "data_missing"),
                                 **{row["ts_code"]: row["status"] for row in results}}
        try:
            report["follow_up"] = build_followup(report)
        except Exception as error:
            logger.warning("盘中历史标的跟踪暂缺: %s", type(error).__name__)
        write_json(folder / "report.json", report)
        (folder / "candidates.csv").write_text(export_csv(candidates), encoding="utf-8-sig")
        if render_static:
            for i, row in enumerate(candidates, 1):
                make_chart(row, snapshot, cutoff, daily_cutoff, folder)
                if i % 10 == 0 or i == len(candidates):
                    logger.info("盘中候选图表 %s/%s", i, len(candidates))
        links = ''.join(f'<tr><td>{escape(r["name"])}</td><td>{r["ts_code"]}</td><td>{r["score"]}</td><td>{escape(r.get("context_pattern") or r["pattern"])}</td><td>{r["risk_pct"]:.2f}%</td></tr>' for r in candidates)
        charts = ''.join(f'<section><h2>{escape(r["name"])} · {r["score"]}分</h2><p>{escape(r["reason"])}</p><img src="charts/{r["ts_code"]}.png" style="width:100%;height:auto"></section>' for r in candidates) if render_static else '<p>交互K线请打开项目「股票行情 → 盘中观察」。</p>'
        html = f'<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><body style="background:#11100f;color:#e6dac5;font-family:Microsoft YaHei,Arial;max-width:1100px;margin:30px auto;padding:20px"><h1>盘中60F观察 · {escape(cutoff)}</h1><p>{escape(report["note"])}</p><p>股票池{audit["total"]}，已完成前缀{audit["complete"]}，候选{len(candidates)}。{escape(report["score_scope"])}</p><table cellpadding="10">{links}</table>{charts}</body></html>'
        (folder / "report.html").write_text(html, encoding="utf-8")
        publish_report(report)
        return {"run_id": report["run_id"], "collected": audit["complete"], "candidates": len(candidates), "new_today": sum(r["new_today"] for r in candidates), "statuses": report["statuses"]}


def main():
    parser = argparse.ArgumentParser(description="已完成60F前缀的独立盘中观察，不覆盖盘后看板")
    parser.add_argument("--day", default=str(datetime.now(ZoneInfo("Asia/Shanghai")).date()))
    parser.add_argument("--cutoff", default="11:30", choices=SLOTS)
    parser.add_argument("--wait", action="store_true")
    parser.add_argument("--render-static", action="store_true", help="额外导出静态PNG；前端使用可交互K线")
    args = parser.parse_args()
    print(json.dumps(run(args.day, args.cutoff, args.wait, args.render_static)))


if __name__ == "__main__":
    main()
