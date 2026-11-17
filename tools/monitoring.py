"""Coalesce concurrent monitor probes without caching authentication or roles."""

import threading
import time

from storage.repositories.monitoring import database_state

_CACHE_SECONDS = 10
_lock = threading.Lock()
_expires = 0.0
_cached = None


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
            result = {"ok": False, "error": str(error)[:80]}
        _cached = result
        _expires = time.monotonic() + _CACHE_SECONDS
        return result
