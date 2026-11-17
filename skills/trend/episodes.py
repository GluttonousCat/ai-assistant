"""Group repeated daily selections into stable, first-observed opportunities."""
from hashlib import sha256
from datetime import datetime
import json
from zoneinfo import ZoneInfo

from skills.trend.digest import select_stocks
from skills.trend.tracking import completed_end, performance
from tools.market.trend_data import scan_daily_inputs, tracking_calendar, tracking_history

ARCHIVE_SESSIONS = 20


def _origin(row):
    value = row.get("trend60", {}).get("origin") or row.get("startup", {}).get("start")
    if not value:
        return f'unknown:{row.get("pattern") or row.get("family") or ""}'
    try:
        stamp = datetime.fromisoformat(str(value))
        if stamp.tzinfo:
            stamp = stamp.astimezone(ZoneInfo("Asia/Shanghai")).replace(tzinfo=None)
        return stamp.isoformat()
    except ValueError:
        return str(value).replace(" ", "T")


def group_episodes(snapshots):
    episodes, previous, seen_codes = [], {}, set()
    for index, snapshot in enumerate(sorted(snapshots, key=lambda s: str(s["as_of"]))):
        day = str(snapshot["as_of"])[:10]
        source = json.dumps([snapshot.get("version"), snapshot["metadata"]["config"]], sort_keys=True, default=str)
        current = {}
        for row in select_stocks(snapshot):
            code = row["ts_code"]
            origin = _origin(row)
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
