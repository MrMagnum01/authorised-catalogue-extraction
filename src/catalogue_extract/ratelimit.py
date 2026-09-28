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
    def __init__(self, min_interval_s: float = 1.0, jitter_s: float = 0.3, sleep_fn=time.sleep, clock=time.monotonic):
        self.min_interval_s = min_interval_s
        self.jitter_s = jitter_s
        self._sleep = sleep_fn
        self._clock = clock
        self._last_request_at: Optional[float] = None

    def wait(self) -> float:
        now = self._clock()
        if self._last_request_at is None:
            waited = 0.0
        else:
            target = self._last_request_at + self.min_interval_s + random.uniform(0, self.jitter_s)
            waited = max(0.0, target - now)
            if waited > 0:
                self._sleep(waited)
        self._last_request_at = self._clock()
        return waited


class RetryBudget:
    """A shared, monotonically-decreasing pool of seconds available for retry waits."""

    def __init__(self, total_seconds: float = 120.0, clock=time.monotonic):
        self._remaining = total_seconds
        self._clock = clock

    @property
    def remaining(self) -> float:
        return self._remaining

    def can_afford(self, seconds: float) -> bool:
        return seconds <= self._remaining

    def spend(self, seconds: float) -> None:
        self._remaining = max(0.0, self._remaining - seconds)


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


def backoff_delay(attempt_no: int, base: float = 1.0, cap: float = 30.0) -> float:
    exp = min(cap, base * (2 ** (attempt_no - 1)))
    return exp + random.uniform(0, base)
