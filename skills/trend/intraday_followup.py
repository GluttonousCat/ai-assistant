"""Track earlier candidates against a published, closed 60F round."""
import math

from skills.trend.episodes import ARCHIVE_SESSIONS, group_episodes
from tools.market.trend_data import (
    intraday_report, load_dashboard, scan_daily_inputs, scan_inputs, tracking_calendar, tracking_history,
)


def _stamp(value):
    return str(value).replace("T", " ")[:19]


def _positive(value):
    try:
        value = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if math.isfinite(value) and value > 0 else None


def _bars(frame):
    return {(row["ts_code"], _stamp(row["bar_end"])): row for row in frame.to_dict("records")}


def _status(report, candidates, code):
    if code in candidates:
        return "candidate"
    return (report or {}).get("scan_status", {}).get(code)


def _change(before, current, previous, candidate):
    if current == "data_missing":
        return None
    if before == "candidate" and current == "filtered":
        return "退出盘中观察"
    if before == "filtered" and current == "candidate":
        return "转入盘中观察"
    if previous and candidate:
        old = (previous.get("trend60") or {}).get("origin")
        new = (candidate.get("trend60") or {}).get("origin")
        if old and new and _stamp(old) != _stamp(new):
            return "60F 结构起点更新"
        old_kind, new_kind = previous.get("context_pattern"), candidate.get("context_pattern")
        if old_kind and new_kind and old_kind != new_kind:
            return "机会类型变化"
    return None


def _price_context(code, last_date, reference, daily, minute, daily_cutoff, cutoff, prior_cutoff):
    now = minute.get((code, cutoff), {})
    previous = daily.get((code, daily_cutoff), {})
    close, factor = _positive(now.get("close")), _positive(now.get("adj_factor"))
    pre_close, pre_factor = _positive(previous.get("close")), _positive(previous.get("adj_factor"))
    if any(value is None for value in (close, factor, pre_close, pre_factor)):
        return None, None, None, False
    change = round((close * factor / (pre_close * pre_factor) - 1) * 100, 2)
    original = daily.get((code, last_date + " 15:00:00"), {})
    original_factor = _positive(original.get("adj_factor"))
    if reference is None or original_factor is None:
        return change, close, None, False
    level = reference * original_factor / factor
    earlier_close = pre_close * pre_factor / factor
    if prior_cutoff:
        earlier = minute.get((code, prior_cutoff), {})
        earlier_price = _positive(earlier.get("close"))
        earlier_factor = _positive(earlier.get("adj_factor"))
        if earlier_price is not None and earlier_factor is not None:
            earlier_close = earlier_price * earlier_factor / factor
    return change, close, round(level, 4), earlier_close >= level > close


def build_followup(report):
    cutoff = _stamp(report["as_of"])
    daily_cutoff = _stamp(report["daily_as_of"])
    if daily_cutoff[:10] >= cutoff[:10]:
        raise ValueError("盘中跟踪需要此前完整日K")
    snapshots = tracking_history(daily_cutoff)
    source_id = report.get("source_snapshot_run_id")
    if source_id and not any(snapshot["run_id"] == source_id for snapshot in snapshots):
        pinned = load_dashboard(source_id)
        if not pinned or _stamp(pinned["as_of"])[:10] != daily_cutoff[:10]:
            raise ValueError("盘中跟踪历史快照与日K日期不一致")
        snapshots = [snapshot for snapshot in snapshots if _stamp(snapshot["as_of"])[:10] < daily_cutoff[:10]]
        snapshots.append(pinned)
    episodes = group_episodes(snapshots)
    if not episodes:
        return []
    calendar = tracking_calendar(min(row["first_seen"] for row in episodes), daily_cutoff[:10])
    active = {}
    for episode in sorted(episodes, key=lambda row: (row["last_seen"], row["first_seen"])):
        if sum(day > episode["first_seen"] for day in calendar) < ARCHIVE_SESSIONS:
            active[episode["ts_code"]] = episode
    if not active:
        return []
    frozen = {(snap["run_id"], row["ts_code"]): row for snap in snapshots for row in snap["data"]}
    previous_slots = [slot for slot in report.get("slots", []) if slot < cutoff[11:16]]
    prior = intraday_report(f"{cutoff[:10]}_{max(previous_slots).replace(':', '')}") if previous_slots else None
    prior_cutoff = _stamp(prior["as_of"]) if prior else None
    previous = {row["ts_code"]: row for row in (prior or {}).get("data", [])}
    candidates = {row["ts_code"]: row for row in report["data"]}
    codes = sorted(active)
    daily = _bars(scan_daily_inputs(codes, min(row["last_seen"] for row in active.values()), daily_cutoff))
    minute = _bars(scan_inputs(codes, cutoff[:10], cutoff))
    tracked = []
    for code, episode in active.items():
        baseline = frozen[episode["last_run_id"], code]
        current = _status(report, candidates, code)
        change = _change(_status(prior, previous, code), current, previous.get(code) or baseline, candidates.get(code))
        pct, close, support, crossed = _price_context(code, episode["last_seen"], _positive(baseline.get("stop_price")),
                                                     daily, minute, daily_cutoff, cutoff, prior_cutoff)
        support_loss = crossed and current != "data_missing"
        events = [label for label in ("跌破历史参考位" if support_loss else None, change) if label]
        unavailable = "本轮未纳入扫描" if "scan_status" in report else "历史轮次无逐股结果"
        state = {"candidate": "盘中观察", "filtered": "未进入本轮观察", "data_missing": "扫描数据不足"}.get(
            current, unavailable)
        tracked.append({"ts_code": code, "name": episode["name"], "last_seen": episode["last_seen"],
                        "previous_state": baseline.get("state"), "state": state,
                        "scan_verified": current in {"candidate", "filtered"},
                        "events": events, "close": close, "change_pct": pct, "support": support})
    return sorted(tracked, key=lambda row: (not bool(row["events"]), row["change_pct"] is not None,
                                            row["last_seen"] != daily_cutoff[:10], row["ts_code"]))


def new_observations(report):
    return [row for row in report["data"] if row.get("new_today")]
