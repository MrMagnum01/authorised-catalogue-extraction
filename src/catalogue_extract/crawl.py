"""Crawl orchestration: discovery, pagination, accounting, product identity.

Architecture note: product records come exclusively from detail pages;
listing pages only ever yield discovery links (more listing pages, or
detail links). Listing and detail records never overlap.
"""
from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from . import PARSER_VERSION
from .boundary import Scope, canonical_url, is_in_scope
from .fetch import fetch_resource
from .models import PageOutcome, Product, RawProductRecord
from .parse import parse_detail, parse_listing
from .ratelimit import RateLimiter, RetryBudget
from .robots import RobotsResult, can_fetch, fetch_and_parse_robots


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
    permission_sha256: Optional[str]
    permission_fetched_at: Optional[str]
    robots: Optional[RobotsResult]
    start_snapshot: Optional[dict]
    end_snapshot: Optional[dict]
    pages: dict = field(default_factory=dict)
    out_of_scope_urls: set = field(default_factory=set)
    accounting: Accounting = field(default_factory=Accounting)
    products: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    conflicting_ids: dict = field(default_factory=dict)
    exact_duplicates: int = 0
    expected_items: Optional[int] = None
    listing_detail_overlap: bool = False


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

    for seed in (permission_url, robots_url, snapshot_meta_url, seed_listing_url):
        if not seed.startswith(scope.origin):
            return CrawlResult(
                crawl_id=crawl_id, status="refused", reasons=["seed_url_outside_origin"],
                started_at=started_at, ended_at=_now_iso(), scope_origin=scope.origin,
                permission_sha256=None, permission_fetched_at=None, robots=None,
                start_snapshot=None, end_snapshot=None, accounting=accounting,
            )

    accounting.control_requests += 1
    try:
        presp = client.get(permission_url, headers={"User-Agent": config.user_agent}, timeout=10.0)
    except httpx.TransportError:
        return CrawlResult(
            crawl_id=crawl_id, status="refused", reasons=["permission_unreachable"],
            started_at=started_at, ended_at=_now_iso(), scope_origin=scope.origin,
            permission_sha256=None, permission_fetched_at=None, robots=None,
            start_snapshot=None, end_snapshot=None, accounting=accounting,
        )
    if presp.status_code != 200:
        return CrawlResult(
            crawl_id=crawl_id, status="refused", reasons=[f"permission_status_{presp.status_code}"],
            started_at=started_at, ended_at=_now_iso(), scope_origin=scope.origin,
            permission_sha256=None, permission_fetched_at=None, robots=None,
            start_snapshot=None, end_snapshot=None, accounting=accounting,
        )
    permission_sha256 = hashlib.sha256(presp.content).hexdigest()
    permission_fetched_at = _now_iso()

    accounting.control_requests += 1
    robots = fetch_and_parse_robots(client, robots_url)
    if not robots.ok:
        return CrawlResult(
            crawl_id=crawl_id, status="refused", reasons=[f"robots_{robots.reason}"],
            started_at=started_at, ended_at=_now_iso(), scope_origin=scope.origin,
            permission_sha256=permission_sha256, permission_fetched_at=permission_fetched_at,
            robots=robots, start_snapshot=None, end_snapshot=None, accounting=accounting,
        )

    accounting.control_requests += 1
    start_snapshot = _fetch_snapshot_meta(client, snapshot_meta_url, config.user_agent)
    if start_snapshot is None:
        return CrawlResult(
            crawl_id=crawl_id, status="refused", reasons=["snapshot_meta_unreachable"],
            started_at=started_at, ended_at=_now_iso(), scope_origin=scope.origin,
            permission_sha256=permission_sha256, permission_fetched_at=permission_fetched_at,
            robots=robots, start_snapshot=None, end_snapshot=None, accounting=accounting,
        )

    rate_limiter = RateLimiter(config.min_interval_s, config.jitter_s)
    retry_budget = RetryBudget(config.retry_budget_s)

    pages: dict[str, PageOutcome] = {}
    out_of_scope: set = set()
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
            user_agent=config.user_agent,
        )

        if not result.ok:
            pages[canon] = PageOutcome(
                url=url, canonical_url=canon, kind=kind, outcome="failed",
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
            if listing.next_link:
                abs_next = str(httpx.URL(result.final_url).join(listing.next_link))
                if is_in_scope(abs_next, scope):
                    c = canonical_url(abs_next)
                    if c not in queued_canon:
                        queued_canon.add(c)
                        queue.append((abs_next, "listing"))
                else:
                    out_of_scope.add(abs_next)
        else:
            raw_products.append(parse_detail(result.content, result.final_url))

    accounting.parsed = sum(1 for p in pages.values() if p.outcome == "parsed")
    accounting.failed = sum(1 for p in pages.values() if p.outcome == "failed")
    accounting.skipped_by_robots = sum(1 for p in pages.values() if p.outcome == "skipped_by_robots")
    accounting.unattempted_limit = sum(1 for p in pages.values() if p.outcome == "unattempted_limit")
    accounting.out_of_scope_links = len(out_of_scope)
    accounting.discovered_in_scope = len(pages)

    accounting.control_requests += 1
    end_snapshot = _fetch_snapshot_meta(client, snapshot_meta_url, config.user_agent)

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
    if end_snapshot is None or start_snapshot != end_snapshot:
        reasons.append("snapshot_changed_mid_crawl")

    status = "complete" if not reasons else "incomplete"

    return CrawlResult(
        crawl_id=crawl_id, status=status, reasons=sorted(set(reasons)), started_at=started_at,
        ended_at=_now_iso(), scope_origin=scope.origin, permission_sha256=permission_sha256,
        permission_fetched_at=permission_fetched_at, robots=robots,
        start_snapshot=start_snapshot, end_snapshot=end_snapshot, pages=pages,
        out_of_scope_urls=out_of_scope, accounting=accounting, products=products,
        rejected=rejected, conflicting_ids=conflicting, exact_duplicates=exact_dup_count,
        expected_items=(start_snapshot or {}).get("advertised_items"),
    )


def _fetch_snapshot_meta(client: httpx.Client, url: str, user_agent: str) -> Optional[dict]:
    try:
        resp = client.get(url, headers={"User-Agent": user_agent}, timeout=10.0)
    except httpx.TransportError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


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
