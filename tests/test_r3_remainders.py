"""Wired-in regression tests for Astra's HOLD, round 3, remaining
group1 (seed boundary validation), group4 (pagination cycle) and group9
(retry-budget elapsed time, seeded jitter/backoff) clauses.

See vault/40-sessions/2026-09-28-astra-authorised-extraction-r3-review.md
and its probes script for the original findings these reproduce.
"""
from __future__ import annotations

import random
from unittest.mock import patch

import httpx
import pytest

from catalogue_extract.boundary import Scope, canonical_url
from catalogue_extract.crawl import CrawlConfig, run_crawl
from catalogue_extract.fetch import fetch_resource
from catalogue_extract.ratelimit import RateLimiter, RetryBudget

ORIGIN = "http://127.0.0.1:1234"
GRANT = f"Site: {ORIGIN}\nAllowed-Paths: /catalogue/\nPurpose: demo\nPermission: granted\n"


def _control_responses(request: httpx.Request):
    path = request.url.path
    if path == "/PERMISSION.md":
        return httpx.Response(200, text=GRANT)
    if path == "/robots.txt":
        return httpx.Response(200, text="User-agent: *\nAllow: /\n")
    if path == "/_meta/snapshot":
        return httpx.Response(200, json={"snapshot_id": "s", "advertised_items": 0, "advertised_pages": 1})
    return None


def test_malformed_seed_port_is_a_categorised_failed_page_not_a_crash():
    """Astra HOLD r3, group1: `run_crawl` canonicalized the seed before
    boundary validation. `canonical_url` parses the port with no
    BoundaryError conversion (unlike `check_in_scope`), so a malformed
    seed port raised a bare ValueError straight out of `run_crawl`
    instead of the categorised, accounted-for refusal every other
    malformed URL on the crawl path already gets."""
    seed = "http://127.0.0.1:broken/catalogue/"

    def transport(request: httpx.Request) -> httpx.Response:
        resp = _control_responses(request)
        return resp if resp is not None else httpx.Response(200, text="unreachable")

    scope = Scope.from_origin(ORIGIN, ["/catalogue/"])
    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
        result = run_crawl(
            client, scope, seed,
            ORIGIN + "/PERMISSION.md", ORIGIN + "/robots.txt", ORIGIN + "/_meta/snapshot",
            CrawlConfig(min_interval_s=0, jitter_s=0),
        )

    assert result.status == "incomplete"
    seed_page = result.pages[seed]
    assert seed_page.outcome == "failed"
    assert seed_page.reason == "boundary:malformed_port"
    assert result.accounting.failed == 1


def test_self_referencing_next_link_cycle_marks_incomplete():
    """Astra HOLD r3, group4: a required next-page link pointing back at
    an already-seen listing page (here: itself) was silently deduplicated
    by the `queued_canon` check with no completeness consequence — an
    empty catalogue whose only listing page linked to itself reported
    `complete` with advertised_pages=1/advertised_items=0 matching
    exactly. Unlike a repeated product link (legitimately referenced from
    multiple listing pages), a pagination cycle means the crawl never
    reached a natural end and must not be reported complete."""
    def transport(request: httpx.Request) -> httpx.Response:
        resp = _control_responses(request)
        if resp is not None:
            return resp
        return httpx.Response(
            200,
            text='<ul class="product-list"></ul><a class="next-page" href="/catalogue/">Next</a>',
        )

    scope = Scope.from_origin(ORIGIN, ["/catalogue/"])
    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
        result = run_crawl(
            client, scope, ORIGIN + "/catalogue/",
            ORIGIN + "/PERMISSION.md", ORIGIN + "/robots.txt", ORIGIN + "/_meta/snapshot",
            CrawlConfig(min_interval_s=0, jitter_s=0),
        )

    assert result.status == "incomplete"
    assert "required_link_cycle" in result.reasons
    # Exactly the one (self-referencing) listing page was ever fetched.
    assert result.accounting.parsed == 1


def test_retry_wait_refused_when_elapsed_plus_wait_would_exceed_budget():
    """Astra HOLD r3, group9: `RetryBudget.can_afford` only checked the
    caller-driven spent pool, so a slow-but-not-erroring 110s response
    followed by a 100s `Retry-After` under a 120s budget still got
    dispatched — sleeping all the way to t=210s, since no explicit wait
    had been charged yet. The real elapsed time since the budget's first
    use must also be checked before a sleep is ever started: refuse if
    elapsed + wait >= budget."""
    now = [0.0]

    def slow(request: httpx.Request) -> httpx.Response:
        now[0] += 110
        return httpx.Response(429, headers={"retry-after": "100"})

    scope = Scope.from_origin(ORIGIN, ["/catalogue/"])
    with httpx.Client(transport=httpx.MockTransport(slow), trust_env=False) as client, \
            patch("catalogue_extract.fetch.time.monotonic", side_effect=lambda: now[0]), \
            patch("catalogue_extract.fetch.time.sleep", side_effect=lambda s: now.__setitem__(0, now[0] + s)):
        result = fetch_resource(
            client, ORIGIN + "/catalogue/a", scope,
            RateLimiter(0, 0), RetryBudget(120),
        )

    assert not result.ok
    assert result.terminal_reason == "failed:retry_budget_exceeded"
    assert now[0] == 110  # never slept the Retry-After — must never reach t=210
    assert len(result.attempts) == 1


def test_backoff_rng_is_threaded_through_fetch_resource():
    """Astra HOLD r3, group9: `fetch.py`'s backoff calls omitted `rng`
    entirely — `backoff_delay` is seedable as a helper, but nothing in
    `fetch_resource` ever passed a seed through to it, so production
    backoff jitter was never reproducible even when a seeded `rng` was
    supplied."""
    def _second_attempt_wait(seed):
        calls = {"n": 0}

        def transport(request: httpx.Request) -> httpx.Response:
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(500)
            return httpx.Response(200, content=b"ok")

        scope = Scope.from_origin("http://127.0.0.1:1", ["/catalogue"])
        with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
            result = fetch_resource(
                client, "http://127.0.0.1:1/catalogue/a", scope,
                RateLimiter(0.0, 0.0), RetryBudget(120.0), max_attempts=3,
                rng=random.Random(seed),
            )
        assert result.ok
        return result.attempts[-1].waited_before_s

    a = _second_attempt_wait(11)
    b = _second_attempt_wait(11)
    c = _second_attempt_wait(12)
    assert a == b
    assert a != c


def test_crawl_config_rng_seed_reaches_the_rate_limiter():
    """Astra HOLD r3, group9: `crawl.py` constructed `RateLimiter` without
    ever passing it a seeded rng, so `CrawlConfig`'s jitter was never
    reproducible in production even when a seed was supplied. The same
    seeded rng must reach the shared rate limiter used by every fetch —
    control and page — in the run."""
    def transport(request: httpx.Request) -> httpx.Response:
        resp = _control_responses(request)
        return resp if resp is not None else httpx.Response(200, text='<ul class="product-list"></ul>')

    scope = Scope.from_origin(ORIGIN, ["/catalogue/"])

    def _run(seed):
        with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
            config = CrawlConfig(min_interval_s=0.0, jitter_s=1.0, rng_seed=seed)
            result = run_crawl(
                client, scope, ORIGIN + "/catalogue/",
                ORIGIN + "/PERMISSION.md", ORIGIN + "/robots.txt", ORIGIN + "/_meta/snapshot",
                config,
            )
        page = result.pages[canonical_url(ORIGIN + "/catalogue/")]
        return page.attempts[0].waited_before_s

    a = _run(5)
    b = _run(5)
    c = _run(6)
    # `RateLimiter`'s own clock/sleep defaults are real (its determinism
    # test coverage lives in test_fetch_redirect_and_budget.py, with an
    # injected fake clock) — a tiny amount of genuine wall-clock jitter
    # between otherwise-identical runs is expected and fine; what must
    # hold is that the seeded jitter draw itself, not real-time noise,
    # dominates the value, and that two different seeds diverge well
    # beyond that noise floor.
    assert a == pytest.approx(b, abs=1e-3)
    assert abs(a - c) > 1e-3
