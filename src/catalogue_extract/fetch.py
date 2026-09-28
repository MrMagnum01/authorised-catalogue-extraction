"""Single-URL fetch with bounded retries and manually-revalidated redirects.

`follow_redirects=False` is passed explicitly on every request, so an
injected or misconfigured `httpx.Client` (e.g. one built with
`follow_redirects=True`) can never make httpx itself resolve a
redirect — every hop is instead resolved with `urljoin`, re-checked
against the scope boundary (and, for page fetches, robots), and
tracked against a loop/visited set before it is ever requested. A
redirect that leaves scope terminates the fetch with a boundary
refusal, not a followed request; a redirect into a robots-disallowed
path terminates it with a robots refusal, not a followed request
either.

Every retryable failure — a timeout, a transport error, or a
429/5xx status — is charged against the same shared `RetryBudget`
before it sleeps, and the wait it took is attributed to the *next*
recorded attempt (`waited_before_s`), so no wait is ever spent off the
books. The budget's real monotonic deadline is also rechecked
immediately before every dispatch (not just before a backoff sleep),
so a slow-but-not-erroring response that itself burns the whole budget
still stops the next retry from ever being sent.
"""
from __future__ import annotations

import hashlib
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
RESPONSE_HEADER_ALLOWLIST = {"content-type", "content-length", "location", "retry-after"}


def _header_subset(resp: httpx.Response) -> dict:
    return {k: v for k, v in resp.headers.items() if k.lower() in RESPONSE_HEADER_ALLOWLIST}


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
    allowed_exact_paths: Optional[frozenset] = None,
) -> FetchResult:
    """Fetch `url`, following redirects manually.

    `require_prefix=False` is used for the fixed control endpoints
    (permission, robots.txt, snapshot-meta): they must still be the
    exact, single authorised origin, but are not required to fall under
    the crawl's own `allowed_prefixes`. `allowed_exact_paths`, when
    given alongside it, further binds those control fetches (including
    their redirect hops) to the crawl's known fixed control paths, so a
    control endpoint can't redirect to an arbitrary same-origin path.

    `robots`, when given, is rechecked against every hop (including the
    first) before it is requested — a redirect into a disallowed path is
    refused with `robots_denied=True`, not followed.
    """
    try:
        check_in_scope(url, scope, require_prefix=require_prefix, allowed_exact_paths=allowed_exact_paths)
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
            now = time.monotonic()
            retry_budget.mark_start(now)
            if retry_budget.expired(now):
                return FetchResult(False, current_url, None, None, attempts, "failed:retry_budget_exceeded", redirect_chain)
            start = now
            try:
                resp = client.get(
                    current_url, headers={"User-Agent": user_agent}, timeout=timeout,
                    follow_redirects=False,
                )
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
            body = resp.content
            body_sha256 = hashlib.sha256(body).hexdigest() if body else None
            attempts.append(FetchAttempt(
                global_attempt, current_url, status, None, elapsed, waited_before,
                response_headers=_header_subset(resp), body_sha256=body_sha256, body=body,
            ))

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
                content = body
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
            check_in_scope(next_url, scope, require_prefix=require_prefix, allowed_exact_paths=allowed_exact_paths)
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
