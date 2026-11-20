"""Best-effort, cross-process task observations; never used to schedule work."""

from contextvars import ContextVar
from datetime import datetime, timezone
from functools import wraps
import os
from pathlib import Path
import re
import threading
from uuid import uuid4

from core.config import get_config
from core.local_state import file_lock, write_json
from core.offpeak import OffPeakEnded

_current = ContextVar("observed_task", default=None)
_SECRET = re.compile(
    r"(?i)([\"']?(?:[\w.-]*(?:password|passwd|secret|token|api[_-]?key|cookie|authorization))[\"']?\s*[:=]\s*)([^\n]+)"
)
_SECRET_NAME = re.compile(
    r"password|passwd|secret|token|api_?key|cookie|auth_?code", re.I
)


def secret_values():
    return tuple(
        value
        for key, value in os.environ.items()
        if len(value) >= 6 and _SECRET_NAME.search(key)
    )


def redact(value, *, secrets=None):
    text = str(value)
    # Match actual configured secrets as well as key/value diagnostics. Never
    # enumerate configuration values into API responses.
    for secret in secret_values() if secrets is None else secrets:
        text = text.replace(secret, "[已脱敏]")
    text = re.sub(r"(?i)\bBearer\s+\S+", "Bearer [已脱敏]", text)
    text = re.sub(r"(\w+://)[^\s/@]+:[^\s/@]+@", r"\1[已脱敏]@", text)
    text = re.sub(r"\beyJ[\w-]+\.[\w-]+\.[\w-]+", "[已脱敏]", text)
    return _SECRET.sub(r"\1[已脱敏]", text)[:4000]


def runs_root():
    return Path(get_config().root_dir) / "output" / "observability" / "runs"


def _now():
    return datetime.now(timezone.utc).isoformat()


class TaskObservation:
    def __init__(self, key, label):
        self.key = uuid4().hex
        self.lock = threading.Lock()
        parent = _current.get()
        self.row = dict(
            id=self.key,
            kind=key,
            label=label,
            pid=os.getpid(),
            parent_id=parent.key if parent else None,
            status="running",
            started_at=_now(),
            updated_at=_now(),
            stage="开始执行",
            events=[],
        )

    def update(self, stage=None, *, completed=None, total=None, warning=None, **values):
        try:
            with self.lock:
                if stage is not None:
                    self.row.update(
                        stage=redact(stage), completed=completed, total=total
                    )
                elif completed is not None:
                    self.row["completed"] = completed
                if total is not None:
                    self.row["total"] = total
                if warning:
                    self.row["warning"] = redact(warning)
                self.row.update(values, updated_at=_now())
                if stage is not None or warning:
                    self.row["events"] = (
                        self.row["events"]
                        + [
                            {
                                "at": self.row["updated_at"],
                                "text": redact(warning or stage),
                            }
                        ]
                    )[-40:]
                write_json(runs_root() / f"{self.key}.json", self.row)
        except Exception:
            pass  # Observability must not break an import, download or transaction.


def task_progress(stage=None, **values):
    current = _current.get()
    if current:
        current.update(stage, **values)


def observed(key, label):
    """Observe synchronous work units, including CLI and thread-pool executions."""

    def decorate(func):
        @wraps(func)
        def run(*args, **kwargs):
            # Acquiring a lease is best-effort too; the OS releases it on crash.
            try:
                task = TaskObservation(key, label)
                lease = file_lock(runs_root() / f"{task.key}.lock")
                acquired = lease.__enter__()
            except Exception:
                return func(*args, **kwargs)
            if not acquired:
                lease.__exit__(None, None, None)
                return func(*args, **kwargs)
            parent = _current.get()
            token = _current.set(task)
            try:
                task.update("开始执行")
                try:
                    write_json(
                        runs_root().parent / "active" / f"{task.key}.json",
                        {"id": task.key},
                    )
                except Exception:
                    pass
                result = func(*args, **kwargs)
                state = result.get("status") if isinstance(result, dict) else None
                error = result.get("error") if isinstance(result, dict) else None
                if error or state in ("error", "failed"):
                    status = "failed"
                elif state == "partial" or task.row.get("warning"):
                    status = "partial"
                elif state in ("busy", "waiting", "retry_wait", "exhausted"):
                    status = "skipped"
                else:
                    status = "completed"
                task.update(warning=error, status=status, ended_at=_now())
                if parent and status in ("partial", "failed"):
                    parent.update(warning=f"{label}未完全成功，请查看子任务")
                return result
            except OffPeakEnded:
                task.update("等待下个模型闲时窗口", status="skipped", ended_at=_now())
                raise
            except BaseException as error:
                task.update(
                    warning=f"{type(error).__name__}: {error}",
                    status="failed",
                    ended_at=_now(),
                )
                if parent:
                    parent.update(warning=f"{label}执行失败，请查看子任务")
                raise
            finally:
                _current.reset(token)
                try:
                    (runs_root().parent / "active" / f"{task.key}.json").unlink(
                        missing_ok=True
                    )
                except Exception:
                    pass
                try:
                    lease.__exit__(None, None, None)
                except Exception:
                    pass

        return run

    return decorate
