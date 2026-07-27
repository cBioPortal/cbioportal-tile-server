"""Small in-process rate limiter for expensive API requests."""

from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class RequestRateLimiter:
    def __init__(self, max_clients: int = 10_000) -> None:
        self._requests: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()
        self._max_clients = max_clients

    def allow(self, key: str, limit: int, window_seconds: int = 60) -> bool:
        if limit <= 0:
            return True

        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            timestamps = self._requests[key]
            while timestamps and timestamps[0] <= cutoff:
                timestamps.popleft()
            if len(timestamps) >= limit:
                return False
            timestamps.append(now)
            if len(self._requests) > self._max_clients:
                self._requests.pop(next(iter(self._requests)))
            return True


EXPENSIVE_PATH_PREFIXES = ("/patient/", "/slides/", "/search", "/tiles/")
rate_limiter = RequestRateLimiter()
