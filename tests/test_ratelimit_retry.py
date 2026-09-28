from datetime import datetime, timezone

import httpx
import pytest

from catalogue_extract.boundary import Scope
from catalogue_extract.fetch import fetch_resource
from catalogue_extract.ratelimit import RateLimiter, RetryBudget, backoff_delay, parse_retry_after
from catalogue_fixture.data import generate_products
from catalogue_fixture.site import FixtureServer, FixtureState


def test_parse_retry_after_seconds():
    assert parse_retry_after("5") == 5.0


def test_parse_retry_after_http_date():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc)
    future = "Thu, 01 Jan 2026 00:00:10 GMT"
    assert parse_retry_after(future, now=now) == pytest.approx(10.0, abs=0.1)


def test_parse_retry_after_invalid_returns_none():
    assert parse_retry_after("not-a-value") is None
    assert parse_retry_after(None) is None


def test_rate_limiter_enforces_min_interval():
    calls = {"t": 0.0}
    sleeps = []

    def fake_sleep(s):
        sleeps.append(s)
        calls["t"] += s

    def fake_clock():
        return calls["t"]

    limiter = RateLimiter(min_interval_s=1.0, jitter_s=0.0, sleep_fn=fake_sleep, clock=fake_clock)
    limiter.wait()
    limiter.wait()
    assert sleeps == [1.0]


def test_retry_budget_cannot_afford_excess():
    budget = RetryBudget(total_seconds=5.0)
    assert budget.can_afford(3.0)
    budget.spend(3.0)
    assert not budget.can_afford(3.0)
    assert budget.remaining == pytest.approx(2.0)


def _client():
    return httpx.Client(trust_env=False)


def test_transient_503_then_success_retried_within_budget():
    state = FixtureState(products=generate_products(n=1))
    state.add_injection("/catalogue/", 503, times=2, retry_after="0")
    with FixtureServer(state) as server, _client() as client:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = fetch_resource(
            client, server.origin + "/catalogue/", scope,
            RateLimiter(0.01, 0.0), RetryBudget(30.0),
        )
        assert result.ok
        assert len(result.attempts) == 3


def test_retry_budget_exceeded_fails_without_partial_wait():
    state = FixtureState(products=generate_products(n=1))
    state.add_injection("/catalogue/", 503, times=5, retry_after="1000")
    with FixtureServer(state) as server, _client() as client:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = fetch_resource(
            client, server.origin + "/catalogue/", scope,
            RateLimiter(0.01, 0.0), RetryBudget(2.0),
        )
        assert not result.ok
        assert result.terminal_reason == "failed:retry_budget_exceeded"
        assert len(result.attempts) == 1


def test_max_attempts_exhausted():
    state = FixtureState(products=generate_products(n=1))
    state.add_injection("/catalogue/", 500, times=10)
    with FixtureServer(state) as server, _client() as client:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = fetch_resource(
            client, server.origin + "/catalogue/", scope,
            RateLimiter(0.01, 0.0), RetryBudget(120.0), max_attempts=5,
        )
        assert not result.ok
        assert result.terminal_reason == "failed:retries_exhausted"
        assert len(result.attempts) == 5


def test_404_is_not_retried():
    state = FixtureState(products=[])
    with FixtureServer(state) as server, _client() as client:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = fetch_resource(
            client, server.origin + "/catalogue/product/does-not-exist", scope,
            RateLimiter(0.01, 0.0), RetryBudget(30.0),
        )
        assert not result.ok
        assert result.terminal_reason == "failed:not_found"
        assert len(result.attempts) == 1


def test_redirect_loop_detected():
    state = FixtureState(products=[])
    with FixtureServer(state) as server, _client() as client:
        scope = Scope.from_origin(server.origin, ["/catalogue", "/_redirect"])
        result = fetch_resource(
            client, server.origin + "/_redirect/loop", scope,
            RateLimiter(0.01, 0.0), RetryBudget(30.0),
        )
        assert not result.ok
        assert result.terminal_reason == "failed:redirect_loop"


def test_redirect_outside_origin_refused_without_following():
    state = FixtureState(products=[])
    with FixtureServer(state) as server, _client() as client:
        scope = Scope.from_origin(server.origin, ["/catalogue", "/_redirect"])
        result = fetch_resource(
            client, server.origin + "/_redirect/outside", scope,
            RateLimiter(0.01, 0.0), RetryBudget(30.0),
        )
        assert not result.ok
        assert result.boundary_reason == "host_mismatch"


def test_response_too_large_rejected():
    state = FixtureState(products=generate_products(n=1))
    with FixtureServer(state) as server, _client() as client:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = fetch_resource(
            client, server.origin + "/catalogue/", scope,
            RateLimiter(0.01, 0.0), RetryBudget(30.0), max_response_bytes=5,
        )
        assert not result.ok
        assert result.terminal_reason == "failed:response_too_large"


def test_backoff_delay_grows_and_caps():
    assert backoff_delay(1, base=1.0, cap=10.0) < backoff_delay(3, base=1.0, cap=10.0) + 1.0
    assert backoff_delay(20, base=1.0, cap=10.0) <= 11.0
