"""Fixed-rate pacing, jitter, and a shared retry-time budget.

One concurrent request, always. Callers serialize by construction
(the crawler issues requests one at a time) — this module only adds
the *pacing* between them plus the retry-budget accounting the brief
requires: a global ceiling on time spent retrying, so a slow/broken
endpoint cannot stall the run indefinitely.
"""
from __future__ import annotations

import random
import time
from email.utils import parsedate_to_datetime
from datetime import datetime, timezone
from typing import Optional


class RateLimiter:
    def __init__(
        self, min_interval_s: float = 1.0, jitter_s: float = 0.3, sleep_fn=time.sleep, clock=time.monotonic,
        rng: Optional[random.Random] = None,
    ):
        self.min_interval_s = min_interval_s
        self.jitter_s = jitter_s
        self._sleep = sleep_fn
        self._clock = clock
        self._rng = rng if rng is not None else random.Random()
        self._last_request_at: Optional[float] = None

    def wait(self) -> float:
        now = self._clock()
        if self._last_request_at is None:
            waited = 0.0
        else:
            target = self._last_request_at + self.min_interval_s + self._rng.uniform(0, self.jitter_s)
            waited = max(0.0, target - now)
            if waited > 0:
                self._sleep(waited)
        self._last_request_at = self._clock()
        return waited


class RetryBudget:
    """A shared, monotonically-decreasing pool of seconds available for retry waits.

    `can_afford`/`spend`/`remaining` are the caller-driven pool (a wait is
    only ever charged when the caller decides to sleep for one). That
    alone misses time spent *outside* an explicit sleep — a slow request,
    or the pacing between requests — so `mark_start`/`elapsed`/`expired`
    additionally track a real monotonic deadline from the budget's first
    use: the caller re-checks `expired(now)` immediately before every
    dispatch (not just before a backoff sleep), using its own clock
    reading, so this class never has to read the clock itself — which
    matters for deterministic tests that patch `time.monotonic` in the
    caller's module rather than this one.
    """

    def __init__(self, total_seconds: float = 120.0):
        self._total = total_seconds
        self._remaining = total_seconds
        self._deadline_start: Optional[float] = None

    @property
    def remaining(self) -> float:
        return self._remaining

    def can_afford(self, seconds: float, now: Optional[float] = None) -> bool:
        """Refuse a wait the caller-driven pool alone would still allow.

        `seconds <= self._remaining` alone only reflects time already
        spent sleeping; it says nothing about a slow-but-not-erroring
        round trip that already burned most of the real deadline. When
        `now` is given, also refuse if `elapsed(now) + seconds` would
        reach or exceed the total budget — a 110s response plus a 100s
        `Retry-After` under a 120s budget must never be allowed to sleep
        to 210s just because no explicit wait had been charged yet.
        """
        if seconds > self._remaining:
            return False
        if now is not None and self.elapsed(now) + seconds >= self._total:
            return False
        return True

    def spend(self, seconds: float) -> None:
        self._remaining = max(0.0, self._remaining - seconds)

    def mark_start(self, now: float) -> None:
        if self._deadline_start is None:
            self._deadline_start = now

    def elapsed(self, now: float) -> float:
        if self._deadline_start is None:
            return 0.0
        return now - self._deadline_start

    def expired(self, now: float) -> bool:
        return self.elapsed(now) >= self._total


def parse_retry_after(value: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """Parse Retry-After as delta-seconds or an HTTP-date (RFC 9110 §10.2.3)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.isdigit():
        return float(value)
    try:
        dt = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    reference = now or datetime.now(timezone.utc)
    return max(0.0, (dt - reference).total_seconds())


def backoff_delay(attempt_no: int, base: float = 1.0, cap: float = 30.0, rng: Optional[random.Random] = None) -> float:
    source = rng if rng is not None else random
    exp = min(cap, base * (2 ** (attempt_no - 1)))
    return exp + source.uniform(0, base)
