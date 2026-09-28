"""Wired-in regression tests for Astra's HOLD, round 2, remaining
group1 (redirect boundary) and group9 (retry-budget deadline) clauses.

See vault/40-sessions/2026-09-28-astra-authorised-extraction-r2-review.md
and its probes script for the original findings these reproduce.
"""
from __future__ import annotations

import random
from unittest.mock import patch

import httpx
import pytest

from catalogue_extract.boundary import Scope
from catalogue_extract.fetch import fetch_resource
from catalogue_extract.ratelimit import RateLimiter, RetryBudget, backoff_delay


def test_injected_client_follow_redirects_true_is_overridden():
    """Group1: a client constructed with follow_redirects=True used to
    let httpx itself resolve the 302 before fetch_resource's manual
    scope/robots recheck ever ran — a same-origin redirect from the
    permitted port straight to an unrelated port went unrefused. The
    fetch must set follow_redirects=False per request, regardless of
    what the injected client was configured with."""
    origin = "http://127.0.0.1:1234"
    scope = Scope.from_origin(origin, ["/catalogue/"])
    seen = []

    def redirect(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.port == 1234:
            return httpx.Response(302, headers={"location": "http://127.0.0.1:9999/escape"})
        return httpx.Response(200, text="escaped")

    with httpx.Client(transport=httpx.MockTransport(redirect), follow_redirects=True, trust_env=False) as client:
        result = fetch_resource(client, origin + "/catalogue/a", scope, RateLimiter(0, 0), RetryBudget())

    assert not result.ok
    assert result.boundary_reason == "port_mismatch"
    # The escape destination must never actually be requested.
    assert not any(":9999" in u for u in seen)
    assert seen == [origin + "/catalogue/a"]


def test_retry_budget_deadline_rechecked_immediately_before_dispatch():
    """Group9: the old RetryBudget only charged explicit backoff sleeps,
    so a single slow-but-not-erroring round trip that itself burned the
    whole budget was invisible to `can_afford` — a retry got dispatched
    at t=131s under a 120s budget. The real monotonic deadline must be
    rechecked right before every dispatch, not just before a sleep."""
    now = [0.0]
    dispatch_times = []

    def slow(request: httpx.Request) -> httpx.Response:
        dispatch_times.append(now[0])
        now[0] += 130
        if len(dispatch_times) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(200, text="ok")

    origin = "http://127.0.0.1:1234"
    scope = Scope.from_origin(origin, ["/catalogue/"])
    with httpx.Client(transport=httpx.MockTransport(slow), trust_env=False) as client, \
            patch("catalogue_extract.fetch.time.monotonic", side_effect=lambda: now[0]), \
            patch("catalogue_extract.fetch.time.sleep", side_effect=lambda s: now.__setitem__(0, now[0] + s)), \
            patch("catalogue_extract.fetch.backoff_delay", return_value=1):
        result = fetch_resource(client, origin + "/catalogue/a", scope, RateLimiter(0, 0), RetryBudget(120))

    assert not result.ok
    assert result.terminal_reason == "failed:retry_budget_exceeded"
    # Only the first (timed-out) dispatch happened; the second retry, due
    # at t=131s under a 120s budget, must never have been sent.
    assert dispatch_times == [0.0]


def test_rate_limiter_jitter_is_seedable_for_deterministic_tests():
    calls = {"t": 0.0}
    limiter_a = RateLimiter(min_interval_s=1.0, jitter_s=0.5, sleep_fn=lambda s: None, clock=lambda: calls["t"], rng=random.Random(42))
    limiter_b = RateLimiter(min_interval_s=1.0, jitter_s=0.5, sleep_fn=lambda s: None, clock=lambda: calls["t"], rng=random.Random(42))
    limiter_a.wait()
    limiter_b.wait()
    calls["t"] = 0.5
    assert limiter_a.wait() == pytest.approx(limiter_b.wait())


def test_backoff_delay_is_seedable_for_deterministic_tests():
    a = backoff_delay(2, rng=random.Random(7))
    b = backoff_delay(2, rng=random.Random(7))
    assert a == b
