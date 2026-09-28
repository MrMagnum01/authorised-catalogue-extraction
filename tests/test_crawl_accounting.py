import httpx

from catalogue_extract.boundary import Scope
from catalogue_extract.crawl import CrawlConfig, run_crawl
from catalogue_fixture.data import generate_products
from catalogue_fixture.site import FixtureServer, FixtureState


def _client():
    return httpx.Client(trust_env=False)


def _run(server, scope, config=None):
    with _client() as client:
        return run_crawl(
            client, scope,
            seed_listing_url=server.origin + "/catalogue/",
            permission_url=server.origin + "/PERMISSION.md",
            robots_url=server.origin + "/robots.txt",
            snapshot_meta_url=server.origin + "/_meta/snapshot",
            config=config or CrawlConfig(min_interval_s=0.02, jitter_s=0.0),
        )


def test_v2_template_crawl_extracts_same_shape_as_v1():
    state = FixtureState(products=generate_products(n=4), template_version="v2")
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = _run(server, scope)
    assert result.status == "complete"
    assert len(result.products) == 4
    assert all(p.template == "v2" for p in result.products)


def test_unrecognised_listing_template_fails_completeness():
    state = FixtureState(products=generate_products(n=2), template_version="unrecognised")
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = _run(server, scope)
    assert result.status == "incomplete"
    assert "template_unrecognised" in result.reasons
    assert result.products == []


def test_complete_crawl_accounts_all_pages_exactly_once():
    state = FixtureState(products=generate_products(n=12), page_size=5)
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = _run(server, scope)
    assert result.status == "complete"
    assert result.accounting.discovered_in_scope == result.accounting.total_in_scope()
    assert len(result.products) == 12
    # 3 listing pages (5+5+2) + 12 detail pages = 15
    assert result.accounting.parsed == 15


def test_page_count_limit_marks_unattempted_not_dropped():
    state = FixtureState(products=generate_products(n=12), page_size=5)
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        config = CrawlConfig(min_interval_s=0.02, jitter_s=0.0, max_page_count=5)
        result = _run(server, scope, config)
    assert result.status == "incomplete"
    assert "unattempted_limit_pages" in result.reasons
    assert result.accounting.unattempted_limit > 0
    assert result.accounting.discovered_in_scope == result.accounting.total_in_scope()


def test_robots_disallowed_detail_page_skipped_not_fetched():
    products = generate_products(n=3)
    products[0].id = "blocked-1"
    state = FixtureState(products=products, robots_disallow=("/catalogue/product/blocked",))
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = _run(server, scope)
    assert result.accounting.skipped_by_robots == 1
    assert result.status == "incomplete"
    assert "robots_denied_pages" in result.reasons
    assert len(result.products) == 2


def test_out_of_scope_bait_links_never_fetched():
    state = FixtureState(products=generate_products(n=2), include_bait_links=True)
    with FixtureServer(state) as other_server:
        state.other_origin = other_server.origin
        with FixtureServer(state) as server:
            scope = Scope.from_origin(server.origin, ["/catalogue", "/_redirect"])
            result = _run(server, scope)
    assert result.status in ("complete", "incomplete")
    assert result.accounting.out_of_scope_links >= 3  # external absolute, protocol-relative, other loopback
    assert not any("192.0.2.1" in c for c in result.pages)  # never turned into a page fetch


def test_duplicate_and_cyclic_links_deduped_by_canonical_url():
    state = FixtureState(products=generate_products(n=3), page_size=10)
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = _run(server, scope)
    # Single listing page (3 items fit page_size=10) + 3 details = 4 pages, no duplicates.
    assert result.accounting.discovered_in_scope == 4


def test_conflicting_id_fails_completeness():
    pid = "SKU-CONFLICT"
    state = FixtureState(
        products=[],
        special_products={
            "twin-a": (
                f'<html><body><div class="product-detail" data-product-id="{pid}">'
                '<h1 class="prod-name">Original Name</h1>'
                '<span class="prod-price" data-currency="USD">100</span>'
                "</div></body></html>"
            ),
            "twin-b": (
                f'<html><body><div class="product-detail" data-product-id="{pid}">'
                '<h1 class="prod-name">Conflicting Name</h1>'
                '<span class="prod-price" data-currency="USD">200</span>'
                "</div></body></html>"
            ),
        },
        extra_listing_links=["/catalogue/product/twin-a", "/catalogue/product/twin-b"],
    )
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = _run(server, scope)
    assert result.status == "incomplete"
    assert "conflicting_id" in result.reasons
    assert pid in result.conflicting_ids
    assert len(result.conflicting_ids[pid]) == 2


def test_exact_duplicate_merges_with_both_sources():
    pid = "SKU-DUP"
    twin_html = (
        f'<html><body><div class="product-detail" data-product-id="{pid}">'
        '<h1 class="prod-name">Mirrored Product</h1>'
        '<span class="prod-price" data-currency="USD">1500</span>'
        '<span class="prod-category">Widgets</span>'
        "</div></body></html>"
    )
    state = FixtureState(
        products=[],
        special_products={"twin-1": twin_html, "twin-2": twin_html},
        extra_listing_links=["/catalogue/product/twin-1", "/catalogue/product/twin-2"],
    )
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = _run(server, scope)
    assert result.status == "complete"
    assert len(result.products) == 1
    assert result.exact_duplicates == 1
    assert len(result.products[0].source_urls) == 2
