"""In-process sliding-window rate limiter, used to throttle failed logins.

Scope note, because this is the kind of thing that is easy to overstate: state
lives in a plain dict in this process. With more than one worker each has its
own counters, so the effective limit multiplies by the worker count, and a
restart clears everything. It raises the cost of online password guessing; it is
not a defence against a distributed attacker. Redis (or the database) is the
right home for this the moment there is a second process.
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque


class SlidingWindowRateLimiter:
    """Allow at most `max_attempts` hits per key within `window_seconds`.

    Only *failed* logins are recorded, so a user typing one wrong password then
    succeeding is never throttled.
    """

    def __init__(self, max_attempts: int, window_seconds: int) -> None:
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _prune(self, key: str, now: float) -> deque[float]:
        """Drop hits that have aged out of the window."""
        bucket = self._hits[key]
        cutoff = now - self.window_seconds
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        return bucket

    def is_limited(self, key: str, now: float | None = None) -> bool:
        """True if `key` has already used up its allowance."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            return len(self._prune(key, moment)) >= self.max_attempts

    def record_failure(self, key: str, now: float | None = None) -> None:
        """Record one failed attempt against `key`."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._prune(key, moment).append(moment)

    def reset(self, key: str) -> None:
        """Clear a key's history, e.g. after a successful login."""
        with self._lock:
            self._hits.pop(key, None)

    def retry_after(self, key: str, now: float | None = None) -> int:
        """Seconds until `key` regains an attempt; 0 if it is not limited."""
        moment = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._prune(key, moment)
            if len(bucket) < self.max_attempts:
                return 0
            return max(1, int(bucket[0] + self.window_seconds - moment) + 1)
