"""Bounded, allowlisted local observations, independent of warehouse health."""

from datetime import datetime, timezone
from pathlib import Path
import re

from core.config import get_config
from core.local_state import file_lock, read_json
from core.logger import log_directory
from core.observability import redact, runs_root, secret_values

LOG_SOURCES = {
    "jobs": "后台任务",
    "tools": "采集与工具",
    "skills": "领域分析",
    "harness": "Agent 引擎",
    "agent": "Agent 历史",
    "api": "API",
    "storage": "存储",
    "core": "基础设施",
    "app": "应用",
    "cli": "命令行",
}
_LEVEL = re.compile(r" - (DEBUG|INFO|WARNING|ERROR|CRITICAL) - ")


def _recent(folder, limit=200):
    return sorted(
        Path(folder).glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True
    )[:limit]


def task_snapshot():
    rows = []
    paths = set(_recent(runs_root()))
    # Long, quiet work must remain visible even after 200 newer short runs.
    for active in (runs_root().parent / "active").glob("*.json"):
        if re.fullmatch(r"[0-9a-f]{32}", active.stem):
            paths.add(runs_root() / active.name)
    for path in paths:
        row = read_json(path)
        if not row or not isinstance(row, dict):
            continue
        if row.get("status") == "running":
            try:
                with file_lock(path.with_suffix(".lock")) as available:
                    if available:
                        row = read_json(path) or row
                        if row.get("status") == "running":
                            row = {
                                **row,
                                "status": "interrupted",
                                "ended_at": row.get("updated_at"),
                                "warning": "执行进程已结束，未留下完成回执；耗时截至最后更新",
                            }
            except OSError:
                row = {**row, "status": "unknown"}
        rows.append(row)
    # The queue already owns durable backtest progress; read a minimal summary.
    root = Path(get_config().root_dir) / "output"
    for path in _recent(root / "quant" / "workspace" / "tasks", 50):
        row = read_json(path)
        if not row:
            continue
        rows.append(
            {
                "id": row.get("id"),
                "kind": "quant",
                "label": "量化回测",
                "stage": redact(row.get("progress", "")),
                "status": row.get("status"),
                "started_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
                "completed": row.get("completed"),
                "total": row.get("total"),
                "events": [],
                "ended_at": row.get("updated_at")
                if row.get("status") not in ("running", "queued")
                else None,
            }
        )
    rows.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    active = [r for r in rows if r.get("status") in ("running", "queued")]
    history = [r for r in rows if r.get("status") not in ("running", "queued")][:60]
    market = read_json(root / "market/refresh/status.json")
    intraday = read_json(root / "market/intraday_watch/scheduler.json")
    mail_paths = sorted(
        (root / "market/email_digest").glob("????-??-??/*/delivery.json"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    mail = read_json(mail_paths[0]) if mail_paths else {}
    receipts = [
        {
            "label": "盘后行情",
            "status": market.get("status"),
            "updated_at": market.get("updated_at"),
            "detail": f"目标交易日 {market.get('target', '—')} · 阶段 {market.get('stage', '—')}",
        },
        {
            "label": "盘中60F",
            "status": intraday.get("status"),
            "updated_at": intraday.get("updated_at"),
            "detail": f"轮次 {intraday.get('run_id', '—')} · 候选 {intraday.get('result', {}).get('candidates', '—')}",
        },
        {
            "label": "趋势邮件",
            "status": mail.get("status"),
            "updated_at": mail.get("sent_at")
            or mail.get("updated_at")
            or mail.get("prepared_at"),
            "detail": f"行情截止 {mail.get('as_of', '—')} · {mail.get('count', '—')} 只候选",
        },
    ]
    return {
        "active": active,
        "history": history,
        "receipts": receipts,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def log_tail(source="jobs", level="ALL", search="", limit=300):
    if source not in LOG_SOURCES or level not in (
        "ALL",
        "DEBUG",
        "INFO",
        "WARNING",
        "ERROR",
        "CRITICAL",
    ):
        raise ValueError("无效的日志来源或级别")
    limit = max(1, min(1000, limit))
    path = log_directory() / f"{source}.log"
    # Reject symlinks escaping the configured log directory.
    if path.resolve().parent != log_directory().resolve():
        raise ValueError("日志文件不在配置目录内")
    try:
        with path.open("rb") as stream:
            size = stream.seek(0, 2)
            start = max(0, size - 262144)
            stream.seek(start)
            raw = stream.read(262144).decode("utf-8", errors="replace")
        if start:
            raw = raw.partition("\n")[2]
    except FileNotFoundError:
        raw, size, start = "", 0, 0
    records, current_level = [], "INFO"
    secrets = secret_values()
    for line in raw.splitlines():
        match = _LEVEL.search(line)
        if match:
            current_level = match.group(1)
        if level == "ALL" or current_level == level:
            records.append((current_level, line))
    lines = []
    for severity, line in reversed(records):
        safe = redact(line, secrets=secrets)
        if search.casefold() in safe.casefold():
            lines.append({"level": severity, "text": safe})
            if len(lines) > limit:
                break
    return {
        "source": source,
        "sources": LOG_SOURCES,
        "lines": list(reversed(lines[:limit])),
        "truncated": bool(start or len(lines) > limit),
        "bytes": size,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
