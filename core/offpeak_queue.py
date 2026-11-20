"""Local durable queue for work owned by the backend's off-peak scheduler."""

from hashlib import sha256
import json
from pathlib import Path

from core.config import get_config
from core.local_state import file_lock, read_json, write_json
from core.logger import get_logger
from core.offpeak import OffPeakEnded, background_models, local_time, next_open

logger = get_logger(__name__)


def queue_root():
    return Path(get_config().root_dir) / "output" / "offpeak_jobs"


def enqueue(kind, **parameters):
    payload = {"kind": kind, "parameters": parameters}
    key = sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:24]
    path = queue_root() / "pending" / f"{key}.json"
    with file_lock(queue_root() / "jobs" / f"{key}.lock") as acquired:
        if acquired and not path.exists():
            write_json(path, {**payload, "created_at": local_time().isoformat(), "attempts": 0})
    return key


def drain(handlers, *, limit=16):
    """Complete eligible units and keep interrupted work for the next window."""
    root = queue_root()
    with file_lock(root / "worker.lock") as acquired:
        if not acquired:
            return 0
        completed = 0
        handled = 0
        paths = sorted((root / "pending").glob("*.json"), key=lambda p: p.stat().st_mtime)
        for path in paths:
            with file_lock(root / "jobs" / f"{path.stem}.lock") as acquired:
                if not acquired:
                    continue
                job = read_json(path, strict=True)
                now = local_time()
                if job.get("retry_at", "") > now.isoformat():
                    continue
                try:
                    with background_models():
                        handlers[job["kind"]](**job["parameters"])
                except OffPeakEnded:
                    break
                except Exception as exc:
                    attempts = job["attempts"] + 1
                    job.update(attempts=attempts, error=type(exc).__name__,
                               retry_at=next_open().isoformat())
                    logger.warning("闲时任务 %s 执行失败 (%s 第 %s 次)", path.stem, job["kind"], attempts)
                    if attempts >= 3:
                        write_json(root / "failed" / path.name, job)
                        path.unlink()
                    else:
                        write_json(path, job)
                else:
                    path.unlink()
                    completed += 1
                handled += 1
                if handled >= limit:
                    break
        return completed
