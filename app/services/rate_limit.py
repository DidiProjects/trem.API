import time
from collections import defaultdict, deque

from app.core.exceptions import RateLimitedError


class SlidingWindowLimiter:
    """At most `limit` hits per key inside `window_seconds`. In memory: the API runs as one process."""

    def __init__(self, limit: int, window_seconds: int, clock=time.monotonic):
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def hit(self, key: str) -> None:
        now = self._clock()
        events = self._hits[key]
        while events and events[0] <= now - self._window:
            events.popleft()
        if len(events) >= self._limit:
            raise RateLimitedError(max(1, int(events[0] + self._window - now) + 1))
        events.append(now)
