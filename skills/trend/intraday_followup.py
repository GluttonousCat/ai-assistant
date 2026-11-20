"""Track earlier candidates against a published, closed 60F round."""
from skills.trend.episode_support import bars_by_close, market_timestamp, positive as _positive, support_exit
from skills.trend.episodes import ARCHIVE_SESSIONS, group_episodes, signal_origin
from tools.market.trend_data import (
    intraday_report, load_dashboard, scan_daily_inputs, scan_inputs, tracking_calendar, tracking_history,
)


def _stamp(value):
    return str(value).replace("T", " ")[:19]


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
        old, new = signal_origin(previous), signal_origin(candidate)
        if old and new and old != new:
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
        if earlier_price is None or earlier_factor is None:
            return change, close, round(level, 4), False
        earlier_close = earlier_price * earlier_factor / factor
    return change, close, round(level, 4), earlier_close >= level > close


def build_followup(report):
    cutoff = _stamp(report["as_of"])
    daily_cutoff = _stamp(report["daily_as_of"])
    if daily_cutoff[:10] >= cutoff[:10]:
        raise ValueError("盘中跟踪需要此前完整日K")
    snapshots = [s for s in tracking_history(daily_cutoff)
                 if (market_timestamp(s.get("created_at")) or str(s["as_of"])) <= cutoff]
    source_id = report.get("source_snapshot_run_id")
    if source_id and not any(snapshot["run_id"] == source_id for snapshot in snapshots):
        pinned = load_dashboard(source_id)
        if not pinned or _stamp(pinned["as_of"])[:10] != daily_cutoff[:10]:
            raise ValueError("盘中跟踪历史快照与日K日期不一致")
        if (market_timestamp(pinned.get("created_at")) or str(pinned["as_of"])) > cutoff:
            raise ValueError("盘中跟踪引用的快照在截止时尚未发布")
        snapshots = [snapshot for snapshot in snapshots if _stamp(snapshot["as_of"])[:10] < daily_cutoff[:10]]
        snapshots.append(pinned)
    episodes = group_episodes(snapshots)
    if not episodes:
        return []
    calendar = tracking_calendar(min(row["first_seen"] for row in episodes), cutoff[:10])
    active = {}
    for episode in sorted(episodes, key=lambda row: (row["last_seen"], row["first_seen"])):
        if sum(episode["first_seen"] < day <= daily_cutoff[:10] for day in calendar) < ARCHIVE_SESSIONS:
            active[episode["ts_code"]] = episode
    if not active:
        return []
    frozen = {(snap["run_id"], row["ts_code"]): row for snap in snapshots for row in snap["data"]}
    configs = {snap["run_id"]: snap["metadata"]["config"] for snap in snapshots}
    previous_slots = [slot for slot in report.get("slots", []) if slot < cutoff[11:16]]
    prior_cutoff = f"{cutoff[:10]} {max(previous_slots)}:00" if previous_slots else None
    previous_day = max((day for day in calendar if day < cutoff[:10]), default=None)
    compare_at = prior_cutoff or (f"{previous_day} 15:00:00" if previous_day else None)
    prior = intraday_report(compare_at[:10] + "_" + compare_at[11:16].replace(":", "")) if compare_at else None
    if prior and (_stamp(prior["as_of"]) != compare_at or prior.get("config") != report.get("config")
                  or prior.get("strategy_version") != report.get("strategy_version")):
        prior = None
    previous = {row["ts_code"]: row for row in (prior or {}).get("data", [])}
    candidates = {row["ts_code"]: row for row in report["data"]}
    codes = sorted(active)
    start = min(row["first_seen"] for row in active.values())
    daily = bars_by_close(scan_daily_inputs(codes, start, daily_cutoff))
    minute = bars_by_close(scan_inputs(codes, start, cutoff))
    tracked = []
    for code, episode in active.items():
        baseline = frozen[episode["last_run_id"], code]
        current = _status(report, candidates, code)
        change = _change(_status(prior, previous, code), current, previous.get(code) or baseline, candidates.get(code))
        pct, close, support, crossed = _price_context(code, episode["first_seen"], _positive(episode.get("stop_price")),
                                                     daily, minute, daily_cutoff, cutoff, prior_cutoff)
        traded = _positive(minute.get((code, cutoff), {}).get("volume")) is not None
        support_loss = crossed and traded and current != "data_missing"
        events = [label for label in ("跌破历史参考位" if support_loss else None, change) if label]
        unavailable = "本轮未纳入扫描" if "scan_status" in report else "历史轮次无逐股结果"
        state = {"candidate": "盘中观察", "filtered": "未进入本轮观察", "data_missing": "扫描数据不足"}.get(
            current, unavailable)
        if current == "data_missing":
            minute.pop((code, cutoff), None)
        retired_at = support_exit(episode, calendar, cutoff, daily, minute)
        if retired_at:
            state = "已移出跟踪"
            events = ["连续两根60F收破参考位，移出本轮"] if retired_at == cutoff else []
        source_matches = (report.get("strategy_version") == episode["source_version"] and
                          report.get("config") == configs[episode["last_run_id"]])
        tracked.append({"ts_code": code, "name": episode["name"], "last_seen": episode["last_seen"],
                        "previous_state": baseline.get("state"), "state": state,
                        "scan_verified": current in {"candidate", "filtered"},
                        "tracking_status": "retired" if retired_at else "active", "retired_at": retired_at,
                        "origin": signal_origin(episode) if source_matches else None,
                        "events": events, "close": close, "change_pct": pct, "support": support})
    return sorted(tracked, key=lambda row: (not bool(row["events"]), not bool(row["retired_at"]),
                                            row["change_pct"] is None, row["ts_code"]))


def apply_followup(report, tracked):
    retired = {r["ts_code"]: r["origin"] for r in tracked if r.get("retired_at") and r.get("origin")}
    kept = []
    for candidate in report["data"]:
        code = candidate["ts_code"]
        if code in retired and retired[code] == signal_origin(candidate):
            report["scan_status"][code] = "filtered"
            report["statuses"]["candidate"] -= 1
            report["statuses"]["filtered"] = report["statuses"].get("filtered", 0) + 1
        else:
            kept.append(candidate)
    report["data"] = kept
    report["follow_up"] = visible_followup(tracked, report["as_of"])


def visible_followup(tracked, as_of):
    return [r for r in tracked if not r.get("retired_at") or r["retired_at"][:10] == str(as_of)[:10]]


def new_observations(report):
    return [row for row in report["data"] if row.get("new_today")]
