from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock


class SimpleRateLimiter:
    """In-process sliding window rate limiter (per key)."""

    def __init__(self) -> None:
        self._events: dict[str, list[float]] = defaultdict(list)
        self._lock = Lock()

    def allow(self, key: str, *, limit: int, window_seconds: float) -> bool:
        now = time.monotonic()
        with self._lock:
            bucket = self._events[key]
            cutoff = now - window_seconds
            while bucket and bucket[0] < cutoff:
                bucket.pop(0)
            if len(bucket) >= limit:
                return False
            bucket.append(now)
            return True


miniapp_rate_limiter = SimpleRateLimiter()
