import time
from collections import defaultdict, deque
from functools import lru_cache
from typing import Optional

from app.core.config import get_settings
from app.core.exceptions import TooManyLoginAttemptsError


class LoginThrottle:
    """
    Failed logins per username and per IP inside a sliding window.

    The username is counted whether or not the account exists: locking only real
    accounts would tell an attacker which usernames are taken. State is in memory,
    like the other per-process stores of this API (it runs as a single process).
    """

    def __init__(self, per_username: int, per_ip: int, window_seconds: int, clock=time.monotonic):
        self._per_username = per_username
        self._per_ip = per_ip
        self._window = window_seconds
        self._clock = clock
        self._by_username: dict[str, deque[float]] = defaultdict(deque)
        self._by_ip: dict[str, deque[float]] = defaultdict(deque)

    def _prune(self, events: deque[float], now: float) -> None:
        while events and events[0] <= now - self._window:
            events.popleft()

    def _retry_after(self, events: deque[float], now: float) -> int:
        return max(1, int(events[0] + self._window - now) + 1)

    def check(self, username: str, ip: Optional[str]) -> None:
        now = self._clock()
        user_events = self._by_username[username.lower()]
        self._prune(user_events, now)
        if len(user_events) >= self._per_username:
            raise TooManyLoginAttemptsError(self._retry_after(user_events, now))
        if ip:
            ip_events = self._by_ip[ip]
            self._prune(ip_events, now)
            if len(ip_events) >= self._per_ip:
                raise TooManyLoginAttemptsError(self._retry_after(ip_events, now))

    def record_failure(self, username: str, ip: Optional[str]) -> None:
        now = self._clock()
        self._by_username[username.lower()].append(now)
        if ip:
            self._by_ip[ip].append(now)

    def reset(self, username: str) -> None:
        self._by_username.pop(username.lower(), None)


@lru_cache()
def get_login_throttle() -> LoginThrottle:
    settings = get_settings()
    return LoginThrottle(
        per_username=settings.LOGIN_MAX_FAILURES_PER_USERNAME,
        per_ip=settings.LOGIN_MAX_FAILURES_PER_IP,
        window_seconds=settings.LOGIN_FAILURE_WINDOW_SECONDS,
    )
