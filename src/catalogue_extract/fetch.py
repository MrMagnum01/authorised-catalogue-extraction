"""Single-URL fetch with bounded retries and manually-revalidated redirects.

httpx's `follow_redirects` is never enabled: every redirect hop is
resolved with `urljoin`, re-checked against the scope boundary (and,
for page fetches, robots), and tracked against a loop/visited set
before it is ever requested. A redirect that leaves scope terminates
the fetch with a boundary refusal, not a followed request; a redirect
into a robots-disallowed path terminates it with a robots refusal, not
a followed request either.

Every retryable failure — a timeout, a transport error, or a
429/5xx status — is charged against the same shared `RetryBudget`
before it sleeps, and the wait it took is attributed to the *next*
recorded attempt (`waited_before_s`), so no wait is ever spent off the
books. If the wait would exceed the remaining budget the fetch fails
immediately with `retry_budget_exceeded` rather than sleeping a
truncated amount and retrying early.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from .boundary import BoundaryError, Scope, check_in_scope
from .models import FetchAttempt
from .ratelimit import RateLimiter, RetryBudget, backoff_delay, parse_retry_after
from .robots import RobotsResult, can_fetch

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
REDIRECT_STATUS = {301, 302, 303, 307, 308}


@dataclass
class FetchResult:
    ok: bool
    final_url: str
    status_code: Optional[int]
    content: Optional[bytes]
    attempts: list[FetchAttempt] = field(default_factory=list)
    terminal_reason: Optional[str] = None
    redirect_chain: list[str] = field(default_factory=list)
    boundary_reason: Optional[str] = None
    robots_denied: bool = False


def fetch_resource(
    client: httpx.Client,
    url: str,
    scope: Scope,
    rate_limiter: RateLimiter,
    retry_budget: RetryBudget,
    *,
    max_attempts: int = 5,
    max_redirects: int = 5,
    max_response_bytes: int = 2_000_000,
    connect_timeout: float = 5.0,
    read_timeout: float = 10.0,
    user_agent: str = "catalogue-extract-demo/1.0",
    require_prefix: bool = True,
    robots: Optional[RobotsResult] = None,
) -> FetchResult:
    """Fetch `url`, following redirects manually.

    `require_prefix=False` is used for the fixed control endpoints
    (permission, robots.txt, snapshot-meta): they must still be the
    exact, single authorised origin, but are not required to fall under
    the crawl's own `allowed_prefixes`.

    `robots`, when given, is rechecked against every hop (including the
    first) before it is requested — a redirect into a disallowed path is
    refused with `robots_denied=True`, not followed.
    """
    try:
        check_in_scope(url, scope, require_prefix=require_prefix)
    except BoundaryError as e:
        return FetchResult(False, url, None, None, [], f"boundary:{e.reason}", [url], e.reason)
    if robots is not None and not can_fetch(robots, url, user_agent):
        return FetchResult(False, url, None, None, [], "robots:disallowed", [url], robots_denied=True)

    timeout = httpx.Timeout(connect=connect_timeout, read=read_timeout, write=read_timeout, pool=read_timeout)
    attempts: list[FetchAttempt] = []
    redirect_chain: list[str] = [url]
    current_url = url
    global_attempt = 0
    carried_wait = 0.0

    while True:
        attempt_no = 0
        while True:
            attempt_no += 1
            global_attempt += 1
            rl_wait = rate_limiter.wait()
            waited_before = rl_wait + carried_wait
            carried_wait = 0.0
            start = time.monotonic()
            try:
                resp = client.get(current_url, headers={"User-Agent": user_agent}, timeout=timeout)
            except httpx.TimeoutException:
                elapsed = time.monotonic() - start
                attempts.append(FetchAttempt(global_attempt, current_url, None, "timeout", elapsed, waited_before))
                if attempt_no >= max_attempts:
                    return FetchResult(False, current_url, None, None, attempts, "failed:timeout", redirect_chain)
                backoff = backoff_delay(attempt_no)
                if not retry_budget.can_afford(backoff):
                    return FetchResult(False, current_url, None, None, attempts, "failed:retry_budget_exceeded", redirect_chain)
                time.sleep(backoff)
                retry_budget.spend(backoff)
                carried_wait = backoff
                continue
            except httpx.TransportError as exc:
                elapsed = time.monotonic() - start
                attempts.append(FetchAttempt(global_attempt, current_url, None, f"transport_error:{exc}", elapsed, waited_before))
                if attempt_no >= max_attempts:
                    return FetchResult(False, current_url, None, None, attempts, "failed:network_error", redirect_chain)
                backoff = backoff_delay(attempt_no)
                if not retry_budget.can_afford(backoff):
                    return FetchResult(False, current_url, None, None, attempts, "failed:retry_budget_exceeded", redirect_chain)
                time.sleep(backoff)
                retry_budget.spend(backoff)
                carried_wait = backoff
                continue

            elapsed = time.monotonic() - start
            status = resp.status_code
            attempts.append(FetchAttempt(global_attempt, current_url, status, None, elapsed, waited_before))

            if status in REDIRECT_STATUS:
                break  # handled by outer loop
            if status == 404:
                return FetchResult(False, current_url, status, None, attempts, "failed:not_found", redirect_chain)
            if status in RETRYABLE_STATUS:
                retry_after = parse_retry_after(resp.headers.get("retry-after"))
                wait_s = retry_after if retry_after is not None else backoff_delay(attempt_no)
                if attempt_no >= max_attempts:
                    return FetchResult(False, current_url, status, None, attempts, "failed:retries_exhausted", redirect_chain)
                if not retry_budget.can_afford(wait_s):
                    return FetchResult(False, current_url, status, None, attempts, "failed:retry_budget_exceeded", redirect_chain)
                time.sleep(wait_s)
                retry_budget.spend(wait_s)
                carried_wait = wait_s
                continue
            if 200 <= status < 300:
                content = resp.content
                if len(content) > max_response_bytes:
                    return FetchResult(False, current_url, status, None, attempts, "failed:response_too_large", redirect_chain)
                return FetchResult(True, current_url, status, content, attempts, None, redirect_chain)
            return FetchResult(False, current_url, status, None, attempts, f"failed:unexpected_status_{status}", redirect_chain)

        # Redirect hop.
        location = resp.headers.get("location")
        if not location:
            return FetchResult(False, current_url, status, None, attempts, "failed:redirect_no_location", redirect_chain)
        next_url = str(httpx.URL(current_url).join(location))
        try:
            check_in_scope(next_url, scope, require_prefix=require_prefix)
        except BoundaryError as e:
            return FetchResult(False, current_url, status, None, attempts, f"boundary:{e.reason}", redirect_chain, e.reason)
        if robots is not None and not can_fetch(robots, next_url, user_agent):
            return FetchResult(False, current_url, status, None, attempts, "robots:disallowed_redirect", redirect_chain, robots_denied=True)
        if next_url in redirect_chain:
            return FetchResult(False, current_url, status, None, attempts, "failed:redirect_loop", redirect_chain)
        if len(redirect_chain) > max_redirects:
            return FetchResult(False, current_url, status, None, attempts, "failed:too_many_redirects", redirect_chain)
        redirect_chain.append(next_url)
        current_url = next_url
