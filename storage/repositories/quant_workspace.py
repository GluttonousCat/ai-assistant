"""Owner-scoped immutable drafts and durable local backtest queue."""

from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from core.config import get_config
from core.local_state import file_lock, read_json, write_json


class QueueBusy(ValueError):
    pass


def _root():
    return Path(get_config().root_dir) / "output" / "quant" / "workspace"


def _owner(owner):
    if type(owner) is not int or owner <= 0:
        raise ValueError("无效用户")
    return str(owner)


def _path(kind, key):
    return _root() / kind / f"{UUID(str(key))}.json"


def _now():
    return datetime.now(timezone.utc).isoformat()


def save_draft(owner, spec):
    row = dict(id=str(uuid4()), owner=_owner(owner), created_at=_now(), spec=spec)
    write_json(_path("drafts", row["id"]), row)
    return row


def _rows(kind):
    return [read_json(p, strict=True) for p in (_root() / kind).glob("*.json")]


def list_drafts(owner):
    return sorted(
        (r for r in _rows("drafts") if r["owner"] == _owner(owner)),
        key=lambda r: r["created_at"],
        reverse=True,
    )[:100]


def enqueue(owner, request):
    owner = _owner(owner)
    key = str(UUID(request["request_id"]))
    with file_lock(_root() / "queue.lock") as acquired:
        if not acquired:
            raise QueueBusy("回测队列正在更新，请稍后重试")
        rows = _rows("tasks")
        existing = next(
            (
                r
                for r in rows
                if r["owner"] == owner and r["request"]["request_id"] == key
            ),
            None,
        )
        if existing:
            if existing["request"] != request:
                raise ValueError("相同提交编号对应不同配置，请重新提交")
            return existing
        draft = read_json(_path("drafts", request["draft_id"]), strict=True)
        if not draft or draft["owner"] != owner:
            raise LookupError("策略不存在")
        if draft["spec"].get("unresolved_requirements"):
            raise ValueError("策略仍含未支持的要求，请先编辑或明确移除后再运行")
        pending = [r for r in rows if r["status"] in ("queued", "running")]
        if len(pending) >= 20 or sum(r["owner"] == owner for r in pending) >= 3:
            raise QueueBusy("未完成回测较多，请等待已有任务完成")
        row = dict(
            id=str(uuid4()),
            owner=owner,
            created_at=_now(),
            updated_at=_now(),
            status="queued",
            progress="等待后台运行",
            request=request,
            spec=draft["spec"],
        )
        write_json(_path("tasks", row["id"]), row)
        return row


def task(owner, task_id):
    row = read_json(_path("tasks", task_id), strict=True)
    return row if row and row["owner"] == _owner(owner) else None


def list_tasks(owner):
    return sorted(
        (r for r in _rows("tasks") if r["owner"] == _owner(owner)),
        key=lambda r: r["created_at"],
        reverse=True,
    )[:100]


def update_task(task_id, **fields):
    path = _path("tasks", task_id)
    row = read_json(path, strict=True)
    if not row:
        raise LookupError("回测任务不存在")
    row.update(fields, updated_at=_now())
    write_json(path, row)


def worker_lock():
    return file_lock(_root() / "worker.lock")


def recover_tasks():
    for row in _rows("tasks"):
        if row["status"] == "running":
            result = read_json(_path("results", row["id"]), strict=True)
            update_task(
                row["id"],
                status="completed" if result else "interrupted",
                progress="回测完成" if result else "服务重启，原任务已中断；可重新提交",
                **({"metrics": result["metrics"]} if result else {}),
            )


def next_task():
    rows = sorted(
        (r for r in _rows("tasks") if r["status"] == "queued"),
        key=lambda r: (r["created_at"], r["id"]),
    )
    return rows[0] if rows else None


def save_result(task_id, result):
    write_json(_path("results", task_id), result)


def load_result(owner, task_id, offset=0, limit=30):
    row = task(owner, task_id)
    if not row or row["status"] != "completed":
        return None
    result = read_json(_path("results", task_id), strict=True)
    if not result:
        return None
    return {
        **result,
        "trades": result["trades"][offset : offset + limit],
        "offset": offset,
        "limit": limit,
    }
