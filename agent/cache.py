"""
Tiny in-process TTL cache. Module-level state so it survives across the per-request
Pipeline instances (each POST /run builds a fresh Pipeline). Single-process only —
fine for this one-service deployment.
"""

import threading
import time
from typing import Any, Callable, Optional

_store: dict[str, tuple[float, Any]] = {}
_lock = threading.Lock()


def get(key: str, ttl: float) -> Optional[Any]:
    """Return the cached value if present and younger than ttl seconds, else None."""
    with _lock:
        item = _store.get(key)
        if item is None:
            return None
        ts, val = item
        if time.time() - ts > ttl:
            _store.pop(key, None)
            return None
        return val


def put(key: str, value: Any) -> None:
    with _lock:
        _store[key] = (time.time(), value)


def get_or_compute(key: str, ttl: float, compute: Callable[[], Any]) -> Any:
    """Return cached value, or compute + cache it. Only caches truthy results so a
    transient failure (None/empty) isn't pinned for the whole TTL."""
    cached = get(key, ttl)
    if cached is not None:
        return cached
    value = compute()
    if value:
        put(key, value)
    return value
