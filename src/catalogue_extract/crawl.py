"""Crawl orchestration: discovery, pagination, accounting, product identity.

Architecture note: product records come exclusively from detail pages;
listing pages only ever yield discovery links (more listing pages, or
detail links). Listing and detail records never overlap.

Permission, robots and the snapshot-meta endpoint are all fetched
through the same guarded `fetch_resource` path used for pages — exact
origin validation, shared pacing, a response-size cap and a bounded,
revalidated redirect chain — with `max_attempts=1` so a control
endpoint's own 429/5xx/timeout still causes an immediate, specific
refusal rather than retrying (robots/permission policy is "refuse the
run", not "retry then refuse"), while still sharing the same
`RateLimiter`/`RetryBudget` instances as page fetches for one
consistent pacing/budget picture per run.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlsplit

import httpx

from . import PARSER_VERSION
from .boundary import Scope, canonical_url, is_in_scope, normalize_path
from .fetch import FetchResult, fetch_resource
from .models import PageOutcome, Product, RawProductRecord
from .parse import parse_detail, parse_listing
from .permission import PermissionGrant, parse_permission, validate_permission_scope
from .ratelimit import RateLimiter, RetryBudget
from .robots import RobotsResult, can_fetch, parse_robots_response


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CrawlConfig:
    user_agent: str = "catalogue-extract-demo/1.0 (+authorised fixture crawl; contact: demo-bot)"
    min_interval_s: float = 1.0
    jitter_s: float = 0.3
    connect_timeout: float = 5.0
    read_timeout: float = 10.0
    max_response_bytes: int = 2_000_000
    max_page_count: int = 200
    max_redirects: int = 5
    max_attempts_per_url: int = 5
    retry_budget_s: float = 120.0


@dataclass
class Accounting:
    discovered_in_scope: int = 0
    parsed: int = 0
    failed: int = 0
    skipped_by_robots: int = 0
    unattempted_limit: int = 0
    out_of_scope_links: int = 0
    control_requests: int = 0

    def total_in_scope(self) -> int:
        return self.parsed + self.failed + self.skipped_by_robots + self.unattempted_limit


@dataclass
class CrawlResult:
    crawl_id: str
    status: str  # "complete" | "incomplete" | "refused"
    reasons: list[str]
    started_at: str
    ended_at: Optional[str]
    scope_origin: str
    allowed_prefixes: tuple
    seed_listing_url: str
    permission_sha256: Optional[str]
    permission_fetched_at: Optional[str]
    permission_site: Optional[str] = None
    permission_allowed_paths: tuple = ()
    permission_purpose: Optional[str] = None
    robots: Optional[RobotsResult] = None
    start_snapshot: Optional[dict] = None
    end_snapshot: Optional[dict] = None
    pages: dict = field(default_factory=dict)
    out_of_scope_urls: set = field(default_factory=set)
    required_out_of_scope_urls: set = field(default_factory=set)
    accounting: Accounting = field(default_factory=Accounting)
    products: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    conflicting_ids: dict = field(default_factory=dict)
    exact_duplicates: int = 0
    expected_items: Optional[int] = None
    listing_detail_overlap: bool = False


def _control_fetch(
    client: httpx.Client, url: str, scope: Scope,
    rate_limiter: RateLimiter, retry_budget: RetryBudget, config: CrawlConfig,
    allowed_exact_paths: frozenset,
) -> FetchResult:
    """Guarded single-attempt fetch for permission/robots/snapshot-meta:
    exact-origin boundary check, shared pacing, response-size cap and a
    bounded, revalidated redirect chain — but no retry, since a control
    endpoint's own transient error must refuse the run, not be retried
    away.

    `allowed_exact_paths` binds every hop (initial request and any
    redirect) to this crawl's own fixed control paths — permission,
    robots.txt, snapshot-meta — so a control endpoint can't redirect to
    some other arbitrary same-origin path and still pass the bare
    exact-origin check `require_prefix=False` alone would allow.
    """
    return fetch_resource(
        client, url, scope, rate_limiter, retry_budget,
        max_attempts=1, max_redirects=config.max_redirects,
        max_response_bytes=config.max_response_bytes,
        connect_timeout=config.connect_timeout, read_timeout=config.read_timeout,
        user_agent=config.user_agent, require_prefix=False,
        allowed_exact_paths=allowed_exact_paths,
    )


def _control_failure_reason(result: FetchResult) -> str:
    reason = result.terminal_reason or "unreachable"
    if reason.startswith("boundary:"):
        return reason
    if reason == "failed:not_found":
        return "status_404"
    if reason == "failed:timeout":
        return "timeout"
    if reason == "failed:network_error":
        return "unreachable"
    if reason == "failed:response_too_large":
        return "response_too_large"
    if reason in ("failed:redirect_loop", "failed:too_many_redirects", "failed:redirect_no_location"):
        return reason.split(":", 1)[1]
    if reason == "failed:retries_exhausted" and result.status_code is not None:
        return f"status_{result.status_code}"
    return reason


def _robots_from_control_fetch(result: FetchResult, fetched_at: str) -> RobotsResult:
    if result.ok:
        return parse_robots_response(result.status_code, result.content, fetched_at)
    reason = result.terminal_reason or ""
    if reason.startswith("boundary:"):
        return RobotsResult(False, f"boundary_{result.boundary_reason}", None, fetched_at, None, None)
    if reason == "failed:timeout":
        return parse_robots_response(None, None, fetched_at, network_error="timeout")
    if reason == "failed:network_error":
        return parse_robots_response(None, None, fetched_at, network_error="unreachable")
    if reason == "failed:not_found":
        return parse_robots_response(404, None, fetched_at)
    if reason == "failed:response_too_large":
        return RobotsResult(False, "response_too_large", result.status_code, fetched_at, None, None)
    if reason in ("failed:redirect_loop", "failed:too_many_redirects", "failed:redirect_no_location"):
        return RobotsResult(False, reason.split(":", 1)[1], result.status_code, fetched_at, None, None)
    # e.g. failed:retries_exhausted at max_attempts=1: classify by the status itself.
    return parse_robots_response(result.status_code, None, fetched_at)


def run_crawl(
    client: httpx.Client,
    scope: Scope,
    seed_listing_url: str,
    permission_url: str,
    robots_url: str,
    snapshot_meta_url: str,
    config: CrawlConfig,
) -> CrawlResult:
    crawl_id = uuid.uuid4().hex[:12]
    started_at = _now_iso()
    accounting = Accounting()
    rate_limiter = RateLimiter(config.min_interval_s, config.jitter_s)
    retry_budget = RetryBudget(config.retry_budget_s)
    control_paths = frozenset(
        normalize_path(urlsplit(u).path or "/")
        for u in (permission_url, robots_url, snapshot_meta_url)
    )

    def refuse(reasons: list[str], **overrides) -> CrawlResult:
        fields = dict(
            crawl_id=crawl_id, status="refused", reasons=reasons,
            started_at=started_at, ended_at=_now_iso(), scope_origin=scope.origin,
            allowed_prefixes=scope.allowed_prefixes, seed_listing_url=seed_listing_url,
            permission_sha256=None, permission_fetched_at=None, accounting=accounting,
        )
        fields.update(overrides)
        return CrawlResult(**fields)

    # --- Permission: a validated grant, not just a 200 status. ---
    accounting.control_requests += 1
    presp = _control_fetch(client, permission_url, scope, rate_limiter, retry_budget, config, control_paths)
    if not presp.ok:
        return refuse([f"permission_{_control_failure_reason(presp)}"])
    permission_sha256 = hashlib.sha256(presp.content).hexdigest()
    permission_fetched_at = _now_iso()
    try:
        permission_text = presp.content.decode("utf-8")
    except UnicodeDecodeError:
        return refuse(["permission_malformed_encoding"],
                       permission_sha256=permission_sha256, permission_fetched_at=permission_fetched_at)
    grant: PermissionGrant = parse_permission(permission_text)
    if not grant.ok:
        return refuse([f"permission_{grant.reason}"],
                       permission_sha256=permission_sha256, permission_fetched_at=permission_fetched_at)
    scope_err = validate_permission_scope(grant, scope)
    if scope_err:
        return refuse([scope_err],
                       permission_sha256=permission_sha256, permission_fetched_at=permission_fetched_at,
                       permission_site=grant.site, permission_allowed_paths=grant.allowed_paths,
                       permission_purpose=grant.purpose)

    # --- Robots: a separate, stricter-than-RFC crawl-policy gate. ---
    accounting.control_requests += 1
    robots_fetched_at = _now_iso()
    robots = _robots_from_control_fetch(
        _control_fetch(client, robots_url, scope, rate_limiter, retry_budget, config, control_paths), robots_fetched_at,
    )
    if not robots.ok:
        return refuse([f"robots_{robots.reason}"],
                       permission_sha256=permission_sha256, permission_fetched_at=permission_fetched_at,
                       permission_site=grant.site, permission_allowed_paths=grant.allowed_paths,
                       permission_purpose=grant.purpose, robots=robots)

    # --- Snapshot metadata: typed, required, used for completeness reconciliation. ---
    accounting.control_requests += 1
    start_snapshot, start_err = _fetch_snapshot_meta(client, snapshot_meta_url, scope, rate_limiter, retry_budget, config, control_paths)
    if start_snapshot is None:
        return refuse([f"snapshot_meta_{start_err}"],
                       permission_sha256=permission_sha256, permission_fetched_at=permission_fetched_at,
                       permission_site=grant.site, permission_allowed_paths=grant.allowed_paths,
                       permission_purpose=grant.purpose, robots=robots)

    pages: dict[str, PageOutcome] = {}
    out_of_scope: set = set()
    required_out_of_scope: set = set()
    raw_products: list[RawProductRecord] = []
    queue: list[tuple] = [(seed_listing_url, "listing")]
    queued_canon = {canonical_url(seed_listing_url)}
    page_limit_hit = False

    while queue:
        url, kind = queue.pop(0)
        canon = canonical_url(url)
        if canon in pages:
            continue
        if page_limit_hit or len(pages) >= config.max_page_count:
            pages[canon] = PageOutcome(url=url, canonical_url=canon, kind=kind, outcome="unattempted_limit")
            page_limit_hit = True
            continue

        if not can_fetch(robots, url, config.user_agent):
            pages[canon] = PageOutcome(url=url, canonical_url=canon, kind=kind, outcome="skipped_by_robots")
            continue

        result = fetch_resource(
            client, url, scope, rate_limiter, retry_budget,
            max_attempts=config.max_attempts_per_url, max_redirects=config.max_redirects,
            max_response_bytes=config.max_response_bytes,
            connect_timeout=config.connect_timeout, read_timeout=config.read_timeout,
            user_agent=config.user_agent, robots=robots,
        )

        if not result.ok:
            outcome_kind = "skipped_by_robots" if result.robots_denied else "failed"
            pages[canon] = PageOutcome(
                url=url, canonical_url=canon, kind=kind, outcome=outcome_kind,
                attempts=result.attempts, final_url=result.final_url, status_code=result.status_code,
                fetch_utc=_now_iso(), reason=result.terminal_reason,
            )
            continue

        content_hash = hashlib.sha256(result.content).hexdigest()
        outcome = PageOutcome(
            url=url, canonical_url=canon, kind=kind, outcome="parsed",
            attempts=result.attempts, final_url=result.final_url, status_code=result.status_code,
            fetch_utc=_now_iso(), content_sha256=content_hash,
        )
        pages[canon] = outcome

        if kind == "listing":
            listing = parse_listing(result.content, result.final_url)
            if listing.template == "unrecognised":
                outcome.reason = "template_unrecognised"
            for link in listing.product_links:
                abs_url = str(httpx.URL(result.final_url).join(link))
                if is_in_scope(abs_url, scope):
                    c = canonical_url(abs_url)
                    if c not in queued_canon:
                        queued_canon.add(c)
                        queue.append((abs_url, "detail"))
                else:
                    out_of_scope.add(abs_url)
                    required_out_of_scope.add(abs_url)
            if listing.next_link:
                abs_next = str(httpx.URL(result.final_url).join(listing.next_link))
                if is_in_scope(abs_next, scope):
                    c = canonical_url(abs_next)
                    if c not in queued_canon:
                        queued_canon.add(c)
                        queue.append((abs_next, "listing"))
                else:
                    out_of_scope.add(abs_next)
                    required_out_of_scope.add(abs_next)
            # `related` links are optional/unrelated (teaser links, or the
            # fixture's own bait links probing for scope escapes): still
            # followed when in-scope, but one resolving out of scope is
            # never a completeness failure the way a required link is.
            for link in listing.related_links:
                abs_url = str(httpx.URL(result.final_url).join(link))
                if is_in_scope(abs_url, scope):
                    c = canonical_url(abs_url)
                    if c not in queued_canon:
                        queued_canon.add(c)
                        queue.append((abs_url, "detail"))
                else:
                    out_of_scope.add(abs_url)
        else:
            # `url` (the original, pre-redirect queue entry) is recorded as the
            # record's source_url so it matches the canonical key `pages` is
            # keyed by; the final (post-redirect) URL/status/hash are only ever
            # available via that page entry, never by re-deriving a key from
            # the final URL, which a redirect would otherwise silently break.
            raw_products.append(parse_detail(result.content, url))

    accounting.parsed = sum(1 for p in pages.values() if p.outcome == "parsed")
    accounting.failed = sum(1 for p in pages.values() if p.outcome == "failed")
    accounting.skipped_by_robots = sum(1 for p in pages.values() if p.outcome == "skipped_by_robots")
    accounting.unattempted_limit = sum(1 for p in pages.values() if p.outcome == "unattempted_limit")
    accounting.out_of_scope_links = len(out_of_scope)
    accounting.discovered_in_scope = len(pages)

    accounting.control_requests += 1
    end_snapshot, end_err = _fetch_snapshot_meta(client, snapshot_meta_url, scope, rate_limiter, retry_budget, config, control_paths)

    products, rejected, conflicting, exact_dup_count = _resolve_products(raw_products)

    reasons: list[str] = []
    for r in rejected:
        reasons.append(r.outcome if r.outcome == "template_unrecognised" else (r.reason or "rejected"))
    if any(p.reason == "template_unrecognised" for p in pages.values()):
        reasons.append("template_unrecognised")
    if conflicting:
        reasons.append("conflicting_id")
    if accounting.failed:
        reasons.append("failed_pages")
    if accounting.unattempted_limit:
        reasons.append("unattempted_limit_pages")
    if accounting.skipped_by_robots:
        reasons.append("robots_denied_pages")
    if required_out_of_scope:
        reasons.append("required_link_out_of_scope")
    if end_snapshot is None or start_snapshot != end_snapshot:
        reasons.append("snapshot_changed_mid_crawl" if end_err is None else f"snapshot_meta_{end_err}")

    listing_pages_parsed = sum(1 for p in pages.values() if p.kind == "listing" and p.outcome == "parsed")
    advertised_pages = start_snapshot.get("advertised_pages")
    advertised_items = start_snapshot.get("advertised_items")
    if listing_pages_parsed != advertised_pages:
        reasons.append("listing_page_count_mismatch")
    if len(products) != advertised_items:
        reasons.append("delivered_item_count_mismatch")

    status = "complete" if not reasons else "incomplete"

    return CrawlResult(
        crawl_id=crawl_id, status=status, reasons=sorted(set(reasons)), started_at=started_at,
        ended_at=_now_iso(), scope_origin=scope.origin, allowed_prefixes=scope.allowed_prefixes,
        seed_listing_url=seed_listing_url, permission_sha256=permission_sha256,
        permission_fetched_at=permission_fetched_at, permission_site=grant.site,
        permission_allowed_paths=grant.allowed_paths, permission_purpose=grant.purpose, robots=robots,
        start_snapshot=start_snapshot, end_snapshot=end_snapshot, pages=pages,
        out_of_scope_urls=out_of_scope, required_out_of_scope_urls=required_out_of_scope,
        accounting=accounting, products=products,
        rejected=rejected, conflicting_ids=conflicting, exact_duplicates=exact_dup_count,
        expected_items=advertised_items,
    )


def _validate_snapshot_meta(meta: object) -> Optional[str]:
    if not isinstance(meta, dict):
        return "not_an_object"
    snapshot_id = meta.get("snapshot_id")
    if not isinstance(snapshot_id, str) or not snapshot_id:
        return "missing_snapshot_id"
    for key in ("advertised_pages", "advertised_items"):
        val = meta.get(key)
        if not isinstance(val, int) or isinstance(val, bool) or val < 0:
            return f"invalid_{key}"
    return None


def _fetch_snapshot_meta(
    client: httpx.Client, url: str, scope: Scope,
    rate_limiter: RateLimiter, retry_budget: RetryBudget, config: CrawlConfig,
    allowed_exact_paths: frozenset,
) -> tuple[Optional[dict], Optional[str]]:
    result = _control_fetch(client, url, scope, rate_limiter, retry_budget, config, allowed_exact_paths)
    if not result.ok:
        return None, _control_failure_reason(result)
    try:
        meta = json.loads(result.content) if result.content else None
    except ValueError:
        return None, "invalid_json"
    err = _validate_snapshot_meta(meta)
    if err:
        return None, err
    return meta, None


def _resolve_products(raw_products: list[RawProductRecord]):
    by_id: dict[str, list[RawProductRecord]] = {}
    rejected: list[RawProductRecord] = []
    for r in raw_products:
        if r.outcome != "accepted":
            rejected.append(r)
            continue
        by_id.setdefault(r.product_id, []).append(r)

    products: list[Product] = []
    conflicting: dict[str, list[RawProductRecord]] = {}
    exact_dup_count = 0
    for pid, records in by_id.items():
        hashes = {r.content_hash for r in records}
        if len(hashes) > 1:
            conflicting[pid] = records
            continue
        first = records[0]
        exact_dup_count += len(records) - 1
        products.append(Product(
            product_id=pid, name=first.name, price=first.price, price_status=first.price_status,
            category=first.category, template=first.template,
            source_urls=[r.source_url for r in records], content_hash=first.content_hash,
            parser_version=PARSER_VERSION,
        ))
    return products, rejected, conflicting, exact_dup_count
