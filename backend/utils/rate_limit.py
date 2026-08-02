"""Login-attempt rate limiting, behind a backend-agnostic interface.

Two implementations:

- `SlidingWindowRateLimiter` -- an in-process dict. State lives in this
  worker's memory: with more than one worker each has its own counters, so the
  effective limit multiplies by the worker count, and a restart clears
  everything. Fine for local development and single-process deployments.
- `RedisSlidingWindowRateLimiter` -- backed by Redis, so the limit means what
  it says across workers and survives a restart.

`get_rate_limiter()` picks one from config, the same way `services/llm.get_llm`
picks an LLM provider: callers depend on the `RateLimiter` interface, never on
which backend is active.
"""
from __future__ import annotations

import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import defaultdict, deque

from backend.utils.config import get_settings
from backend.utils.logging_config import logger


class RateLimiter(ABC):
    """Allow at most `max_attempts` hits per key within `window_seconds`."""

    @abstractmethod
    def is_limited(self, key: str, now: float | None = None) -> bool:
        """True if `key` has already used up its allowance."""

    @abstractmethod
    def record_failure(self, key: str, now: float | None = None) -> None:
        """Record one failed attempt against `key`."""

    @abstractmethod
    def reset(self, key: str) -> None:
        """Clear a key's history, e.g. after a successful login."""

    @abstractmethod
    def retry_after(self, key: str, now: float | None = None) -> int:
        """Seconds until `key` regains an attempt; 0 if it is not limited."""

    @abstractmethod
    def clear_all(self) -> None:
        """Drop every key's history. Test-only isolation hook."""


class SlidingWindowRateLimiter(RateLimiter):
    """In-process sliding window.

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
        moment = time.monotonic() if now is None else now
        with self._lock:
            return len(self._prune(key, moment)) >= self.max_attempts

    def record_failure(self, key: str, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        with self._lock:
            self._prune(key, moment).append(moment)

    def reset(self, key: str) -> None:
        with self._lock:
            self._hits.pop(key, None)

    def retry_after(self, key: str, now: float | None = None) -> int:
        moment = time.monotonic() if now is None else now
        with self._lock:
            bucket = self._prune(key, moment)
            if len(bucket) < self.max_attempts:
                return 0
            return max(1, int(bucket[0] + self.window_seconds - moment) + 1)

    def clear_all(self) -> None:
        with self._lock:
            self._hits.clear()


class RedisSlidingWindowRateLimiter(RateLimiter):
    """Redis-backed sliding window, for deployments with more than one worker.

    Each key is a sorted set: member = one attempt (a unique token so two
    attempts landing on the same timestamp both count), score = the timestamp
    it was recorded at. A key's own TTL is refreshed to `window_seconds` on
    every write, so an abandoned key expires on its own -- no cleanup job.

    Uses wall-clock time (`time.time()`), not `time.monotonic()`: monotonic
    clocks are only comparable within the process that read them, and this
    state is explicitly meant to be shared across processes.
    """

    def __init__(self, redis_client, max_attempts: int, window_seconds: int,
                *, prefix: str = "ratelimit") -> None:
        self._redis = redis_client
        self.max_attempts = max_attempts
        self.window_seconds = window_seconds
        self._prefix = prefix

    def _key(self, key: str) -> str:
        return f"{self._prefix}:{key}"

    def _prune_and_count(self, key: str, now: float) -> int:
        rkey = self._key(key)
        self._redis.zremrangebyscore(rkey, "-inf", now - self.window_seconds)
        return self._redis.zcard(rkey)

    def is_limited(self, key: str, now: float | None = None) -> bool:
        moment = time.time() if now is None else now
        return self._prune_and_count(key, moment) >= self.max_attempts

    def record_failure(self, key: str, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        rkey = self._key(key)
        member = f"{moment}:{uuid.uuid4().hex}"
        pipe = self._redis.pipeline()
        pipe.zadd(rkey, {member: moment})
        pipe.zremrangebyscore(rkey, "-inf", moment - self.window_seconds)
        pipe.expire(rkey, self.window_seconds)
        pipe.execute()

    def reset(self, key: str) -> None:
        self._redis.delete(self._key(key))

    def retry_after(self, key: str, now: float | None = None) -> int:
        moment = time.time() if now is None else now
        if self._prune_and_count(key, moment) < self.max_attempts:
            return 0
        oldest = self._redis.zrange(self._key(key), 0, 0, withscores=True)
        if not oldest:
            return 0
        _, oldest_score = oldest[0]
        return max(1, int(oldest_score + self.window_seconds - moment) + 1)

    def clear_all(self) -> None:
        """Delete every key under this limiter's prefix. Test-only."""
        cursor = 0
        while True:
            cursor, keys = self._redis.scan(cursor=cursor, match=f"{self._prefix}:*")
            if keys:
                self._redis.delete(*keys)
            if cursor == 0:
                return


def get_rate_limiter(max_attempts: int, window_seconds: int, *,
                     prefix: str = "ratelimit") -> RateLimiter:
    """Build the configured rate limiter.

    Redis-backed when `REDIS_URL` is set, so the limit still means something
    once there is more than one worker. Falls back to the in-process limiter if
    `REDIS_URL` is unset, or if it is set but the `redis` package is missing --
    the same "optional dependency, log and degrade" pattern used for
    VECTOR_STORE=chroma.
    """
    settings = get_settings()
    if settings.redis_url:
        try:
            import redis
            client = redis.from_url(settings.redis_url)
            logger.info("Using Redis-backed rate limiter (prefix=%s)", prefix)
            return RedisSlidingWindowRateLimiter(
                client, max_attempts, window_seconds, prefix=prefix)
        except ImportError:
            logger.warning(
                "REDIS_URL is set but the `redis` package is not installed; "
                "falling back to the in-process rate limiter.")
    return SlidingWindowRateLimiter(max_attempts, window_seconds)
