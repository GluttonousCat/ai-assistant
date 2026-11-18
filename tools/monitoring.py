"""Coalesce concurrent monitor probes without caching authentication or roles."""

import threading
import time

from storage.repositories.monitoring import database_state
from storage.repositories.observability import task_snapshot, log_tail
from core.observability import redact

_CACHE_SECONDS = 10
_lock = threading.Lock()
_expires = 0.0
_cached = None
_probe_lock = threading.Lock()
_probe_thread = None


def database_snapshot():
    global _expires, _cached
    with _lock:
        # A monotonic clock is unaffected by wall-clock corrections. The lock
        # coalesces multiple SSE viewers into one database probe per interval.
        if _cached is not None and time.monotonic() < _expires:
            return _cached
        started = time.monotonic()
        try:
            result = {"ok": True, **database_state()}
            result["latency_ms"] = int((time.monotonic() - started) * 1000)
        except Exception as error:
            result = {"ok": False, "error": redact(error)[:200]}
        _cached = result
        _expires = time.monotonic() + _CACHE_SECONDS
        return result


def cached_database_snapshot():
    """One daemon probe at most; a stalled warehouse cannot stall local status."""
    global _probe_thread
    with _probe_lock:
        if (_cached is None or time.monotonic() >= _expires) and (
            _probe_thread is None or not _probe_thread.is_alive()
        ):
            _probe_thread = threading.Thread(
                target=database_snapshot, daemon=True, name="monitor-pg"
            )
            _probe_thread.start()
    result = dict(_cached or {"ok": False, "pending": True})
    if _cached is not None and time.monotonic() >= _expires:
        result["stale"] = True
    return result


def background_snapshot():
    return task_snapshot()


def read_logs(**options):
    return log_tail(**options)
