from datetime import datetime, timezone

import httpx
import pytest

from catalogue_extract.boundary import Scope
from catalogue_extract.crawl import CrawlConfig, run_crawl
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


def test_timeout_retry_spends_shared_budget_and_records_the_wait():
    """Astra HOLD group 9: a timeout retry used to bypass the retry budget
    entirely and its wait was never recorded on any attempt. Both must
    now hold for timeouts exactly as they already did for 429/5xx."""
    calls = {"n": 0}

    def transport(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ReadTimeout("simulated", request=request)
        return httpx.Response(200, content=b"ok")

    scope = Scope.from_origin("http://127.0.0.1:1", ["/catalogue"])
    budget = RetryBudget(120.0)
    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
        result = fetch_resource(
            client, "http://127.0.0.1:1/catalogue/", scope,
            RateLimiter(0.0, 0.0), budget, max_attempts=3,
        )
    assert result.ok
    assert calls["n"] == 2
    assert budget.remaining < 120.0  # the backoff wait after the timeout was actually charged
    assert result.attempts[1].waited_before_s > 0  # ...and recorded against the next attempt


def test_timeout_retry_budget_exceeded_fails_fast_without_exhausting_attempts():
    def transport(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("simulated", request=request)

    scope = Scope.from_origin("http://127.0.0.1:1", ["/catalogue"])
    budget = RetryBudget(0.01)  # smaller than any real backoff_delay(1) (>= 1.0s)
    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
        result = fetch_resource(
            client, "http://127.0.0.1:1/catalogue/", scope,
            RateLimiter(0.0, 0.0), budget, max_attempts=5,
        )
    assert not result.ok
    assert result.terminal_reason == "failed:retry_budget_exceeded"
    assert len(result.attempts) == 1


def test_control_fetch_enforces_response_size_cap():
    """Astra HOLD group 9: control requests (permission/robots/snapshot)
    used to bypass the response-size cap entirely by calling `client.get`
    directly instead of going through `fetch_resource`."""
    origin = "http://127.0.0.1:1"
    scope = Scope.from_origin(origin, ["/catalogue/"])

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/PERMISSION.md":
            return httpx.Response(200, content=b"x" * 100)
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
        result = run_crawl(
            client, scope, origin + "/catalogue/", origin + "/PERMISSION.md",
            origin + "/robots.txt", origin + "/_meta/snapshot",
            CrawlConfig(min_interval_s=0, jitter_s=0, max_response_bytes=10),
        )
    assert result.status == "refused"
    assert result.reasons == ["permission_response_too_large"]


def test_control_fetch_guards_redirect_out_of_scope():
    """A permission/robots/snapshot URL redirecting off-origin must be
    refused, not followed — the same guarded-redirect rule as pages."""
    origin = "http://127.0.0.1:1"
    scope = Scope.from_origin(origin, ["/catalogue/"])

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/PERMISSION.md":
            return httpx.Response(302, headers={"location": "http://192.0.2.1/evil"})
        return httpx.Response(404)

    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
        result = run_crawl(
            client, scope, origin + "/catalogue/", origin + "/PERMISSION.md",
            origin + "/robots.txt", origin + "/_meta/snapshot",
            CrawlConfig(min_interval_s=0, jitter_s=0),
        )
    assert result.status == "refused"
    assert any("host_mismatch" in r for r in result.reasons)
