"""Group repeated daily selections into stable, first-observed opportunities."""
from hashlib import sha256
from datetime import datetime, timedelta
import json
from zoneinfo import ZoneInfo

from skills.trend.digest import select_stocks
from skills.trend.episode_support import bars_by_close, market_timestamp, positive, support_exit
from skills.trend.tracking import completed_end, performance
from tools.market.trend_data import scan_daily_inputs, scan_inputs, tracking_calendar, tracking_history

ARCHIVE_SESSIONS = 20


def signal_origin(row):
    value = (row.get("trend60") or {}).get("origin") or (row.get("startup") or {}).get("start")
    if not value:
        return None
    if len(str(value)) <= 10:
        return None
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo:
            stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        return stamp.isoformat()
    except ValueError:
        return None


def group_episodes(snapshots):
    episodes, previous, seen_codes = [], {}, set()
    for index, snapshot in enumerate(sorted(snapshots, key=lambda s: str(s["as_of"]))):
        day = str(snapshot["as_of"])[:10]
        source = json.dumps([snapshot.get("version"), snapshot["metadata"]["config"]], sort_keys=True, default=str)
        current = {}
        for row in select_stocks(snapshot):
            code = row["ts_code"]
            origin = (signal_origin(row) or (row.get("trend60") or {}).get("origin")
                      or (row.get("startup") or {}).get("start") or f'unknown:{row.get("pattern") or row.get("family") or ""}')
            key = (code, source, origin)
            episode = previous.get(key)
            if episode is None:
                prior_key = next((k for k in previous if k[0] == code), None)
                reason = ("策略版本/参数变化" if prior_key and prior_key[1] != source else
                          "新的60F起点" if prior_key else "间隔后重新入选" if code in seen_codes else
                          "窗口内首次记录" if index == 0 else "首次记录")
                identity = sha256(json.dumps([key, day]).encode()).hexdigest()[:24]
                episode = {**row, "episode_id": identity, "first_seen": day,
                           "first_run_id": snapshot["run_id"], "source_version": snapshot.get("version"),
                           "first_published_at": snapshot.get("created_at"),
                           "round_reason": reason,
                           "window_first": index == 0, "observations": [], "confirmed_at": None}
                episodes.append(episode)
            episode["observations"].append({"date": day, "run_id": snapshot["run_id"],
                "score": row["score"], "eligible": bool(row.get("eligible")), "state": row.get("state"),
                "context_pattern": row.get("context_pattern"), "reason": row.get("reason")})
            if row.get("eligible") and not episode["confirmed_at"]:
                episode["confirmed_at"] = day
            episode.update(last_seen=day, last_run_id=snapshot["run_id"],
                           last_score=row["score"], seen_count=len(episode["observations"]),
                           ever_eligible=episode["confirmed_at"] is not None)
            current[key] = episode
            seen_codes.add(code)
        # An actually saved empty/absent candidate day ends continuity. Missing
        # whole scan days do not invent an exit or a new opportunity.
        previous = current
    latest = max((str(s["as_of"])[:10] for s in snapshots), default=None)
    return [{**episode, "in_latest_scan": episode["last_seen"] == latest} for episode in episodes]


def _exit_times(episodes, calendar, end):
    # Load at most two adjacent 20-session windows per group, including for archive views.
    sessions = {day: i for i, day in enumerate(calendar)}
    cohorts = {}
    for row in episodes:
        index = sessions.get(row["first_seen"])
        if index is not None and positive(row.get("stop_price")) and index < len(calendar) - 1:
            cohorts.setdefault(index // ARCHIVE_SESSIONS, []).append(row)
    exits = {}
    for cohort in cohorts.values():
        start = min(r["first_seen"] for r in cohort)
        last_index = max(sessions[r["first_seen"]] for r in cohort)
        last = calendar[min(last_index + ARCHIVE_SESSIONS, len(calendar) - 1)]
        cutoff = min(end, last + " 15:00:00")
        codes = sorted({r["ts_code"] for r in cohort})
        daily = bars_by_close(scan_daily_inputs(codes, start, cutoff))
        minute = bars_by_close(scan_inputs(codes, start, cutoff))
        for row in cohort:
            index = sessions[row["first_seen"]]
            days = calendar[index:index + ARCHIVE_SESSIONS + 1]
            retired_at = support_exit(row, days, cutoff, daily, minute)
            if retired_at:
                exits[row["episode_id"]] = retired_at
    return exits


def retired_signals(rows, as_of, *, version, config):
    """Reject only still-matching structures from an earlier published selection."""
    origins = {r["ts_code"]: origin for r in rows if (origin := signal_origin(r))}
    if not origins:
        return set()
    end = str(as_of).replace("T", " ")[:19]
    prior = datetime.fromisoformat(end).date() - timedelta(days=1)
    history = [s for s in tracking_history(f"{prior} 15:00:00")
               if (market_timestamp(s.get("created_at")) or str(s["as_of"])) <= end]
    sources = {s["run_id"] for s in history if s.get("version") == version and s["metadata"]["config"] == config}
    matches = [r for r in group_episodes(history) if r["last_run_id"] in sources and r["ts_code"] in origins
               and signal_origin(r) == origins[r["ts_code"]]]
    if not matches:
        return set()
    calendar = tracking_calendar(min(r["first_seen"] for r in matches), end[:10])
    exits = _exit_times(matches, calendar, end)
    return {r["ts_code"] for r in matches if r["episode_id"] in exits}


def episode_tracking(view="active", now=None):
    end = completed_end(now)
    snapshots = tracking_history(end)
    episodes = group_episodes(snapshots)
    first = min((r["first_seen"] for r in episodes), default=end[:10])
    calendar = tracking_calendar(first, end[:10]) if episodes else []
    for episode in episodes:
        age = sum(day > episode["first_seen"] for day in calendar)
        episode["age_sessions"] = age
        episode["archived"] = age >= ARCHIVE_SESSIONS
    inspect = episodes if view == "archive" else [r for r in episodes if not r["archived"]]
    exits = _exit_times(inspect, calendar, end)
    for episode in inspect:
        if episode["episode_id"] in exits:
            episode["retired_at"] = exits[episode["episode_id"]]
            episode["archived"] = True
    selected = [r for r in episodes if r["archived"] == (view == "archive")]
    codes = sorted({r["ts_code"] for r in selected})
    frame = scan_daily_inputs(codes, min(r["first_seen"] for r in selected), end) if selected else None
    groups = {code: part.to_dict(orient="records") for code, part in frame.groupby("ts_code")} if frame is not None and not frame.empty else {}
    rows = [{**r, "follow_up": performance(groups.get(r["ts_code"], []), r["first_seen"], calendar)} for r in selected]
    values = [r["follow_up"]["latest_return_pct"] for r in rows if r["follow_up"]["latest_return_pct"] is not None]
    occurrences = sum(len(select_stocks(snapshot)) for snapshot in snapshots)
    return {"view": view, "dates": [], "data": rows, "as_of": end,
            "latest_scan_date": max((str(s["as_of"])[:10] for s in snapshots), default=None),
            "scan_days": len(snapshots), "merged_occurrences": occurrences - len(episodes),
            "archive_sessions": ARCHIVE_SESSIONS,
            "counts": {"active": sum(not r["archived"] for r in episodes),
                       "archive": sum(r["archived"] for r in episodes)},
            "summary": {"count": len(rows), "stocks": len(codes), "measured": len(values),
                        "positive": sum(v > 0 for v in values),
                        "mean_return_pct": round(sum(values) / len(values), 4) if values else None}}
