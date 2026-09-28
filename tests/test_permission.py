"""Wired-in regression tests for Astra's HOLD, groups 1 and 2:
exact control-URL boundary validation (including the port) and actual
permission-content validation (a 200 response is not itself a grant).

See vault/40-sessions/2026-09-28-astra-authorised-extraction-review.md
and its probes script for the original findings this reproduces.
"""
from __future__ import annotations

import httpx

from catalogue_extract.boundary import Scope
from catalogue_extract.crawl import CrawlConfig, run_crawl
from catalogue_extract.permission import parse_permission, validate_permission_scope
from catalogue_fixture.data import generate_products
from catalogue_fixture.site import FixtureServer, FixtureState

DETAIL_HTML = (
    b'<div class="product-detail" data-product-id="P1"><h1 class="prod-name">One</h1>'
    b'<span class="prod-price" data-currency="USD">100</span>'
    b'<span class="prod-category">A</span></div>'
)


def _mock_run(mode: str):
    """Recreates the astra probe's exact scenario: an allowed origin on
    port 1234, and a permission fetch aimed at port 12345 — which used to
    pass because the old check was `str.startswith(scope.origin)`, and
    "http://127.0.0.1:12345/..." does start with "http://127.0.0.1:1234"."""
    origin = "http://127.0.0.1:1234"
    scope = Scope.from_origin(origin, ["/catalogue/"])
    seen = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        path = request.url.path
        if path == "/PERMISSION.md":
            return httpx.Response(200, text="Explicitly forbidden to crawl")
        if path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\n")
        if path == "/_meta/snapshot":
            return httpx.Response(200, json={"snapshot_id": "s", "advertised_items": 0, "advertised_pages": 1})
        if path == "/catalogue/":
            return httpx.Response(200, text='<ul class="product-list"></ul>')
        return httpx.Response(404)

    permission_url = origin + "5/PERMISSION.md" if mode == "prefix" else origin + "/PERMISSION.md"
    with httpx.Client(transport=httpx.MockTransport(transport), trust_env=False) as client:
        result = run_crawl(
            client, scope, origin + "/catalogue/", permission_url,
            origin + "/robots.txt", origin + "/_meta/snapshot",
            CrawlConfig(min_interval_s=0, jitter_s=0),
        )
    return result, seen


def test_control_url_port_prefix_spoof_is_refused_not_a_match():
    """Group 1: a permission URL on port 12345 must not be accepted just
    because '12345' starts with the allowed origin's '1234'."""
    result, seen = _mock_run("prefix")
    assert result.status == "refused"
    assert any("port_mismatch" in r for r in result.reasons)
    # The spoofed-port URL must never actually be requested.
    assert not any(":12345" in u for u in seen)


def test_denied_permission_body_refuses_despite_http_200():
    """Group 2: PERMISSION.md returning 200 with a body that isn't the
    validated grant grammar (here, plain denial text) must refuse the
    run — a 200 status alone is not authorisation."""
    result, seen = _mock_run("permission")
    assert result.status == "refused"
    assert any(r.startswith("permission_") for r in result.reasons)
    # Nothing past permission validation is ever fetched.
    assert not any("/catalogue/" in u for u in seen)


def test_permission_scope_widening_by_cli_is_rejected():
    origin = "http://127.0.0.1:9"
    scope = Scope.from_origin(origin, ["/catalogue/", "/admin/"])
    grant = parse_permission(f"Site: {origin}\nAllowed-Paths: /catalogue/\nPurpose: demo\nPermission: granted\n")
    assert grant.ok
    assert validate_permission_scope(grant, scope) == "cli_scope_exceeds_permission"


def test_permission_grant_covering_cli_scope_is_accepted():
    origin = "http://127.0.0.1:9"
    scope = Scope.from_origin(origin, ["/catalogue/"])
    grant = parse_permission(f"Site: {origin}\nAllowed-Paths: /catalogue/\nPurpose: demo\nPermission: granted\n")
    assert validate_permission_scope(grant, scope) is None


def test_parse_permission_requires_all_fields():
    assert parse_permission("Site: http://x\nPermission: granted\n").ok is False


def test_parse_permission_denied_value_rejected():
    grant = parse_permission("Site: http://x\nAllowed-Paths: /a/\nPurpose: demo\nPermission: denied\n")
    assert grant.ok is False
    assert grant.reason == "not_granted"


def test_fixture_denied_permission_refuses_full_crawl():
    """End-to-end version against the real fixture server: permission_mode
    'denied' must refuse before a single catalogue page is fetched."""
    state = FixtureState(products=generate_products(n=3), permission_mode="denied")
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        with httpx.Client(trust_env=False) as client:
            result = run_crawl(
                client, scope, server.origin + "/catalogue/", server.origin + "/PERMISSION.md",
                server.origin + "/robots.txt", server.origin + "/_meta/snapshot",
                CrawlConfig(min_interval_s=0.02, jitter_s=0.0),
            )
    assert result.status == "refused"
    assert result.reasons == ["permission_not_granted"]


def test_fixture_narrow_permission_paths_refuses_wider_cli_scope():
    state = FixtureState(products=generate_products(n=3), permission_mode="narrow_paths")
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        with httpx.Client(trust_env=False) as client:
            result = run_crawl(
                client, scope, server.origin + "/catalogue/", server.origin + "/PERMISSION.md",
                server.origin + "/robots.txt", server.origin + "/_meta/snapshot",
                CrawlConfig(min_interval_s=0.02, jitter_s=0.0),
            )
    assert result.status == "refused"
    assert result.reasons == ["cli_scope_exceeds_permission"]


def test_fixture_wrong_site_permission_refuses():
    state = FixtureState(products=generate_products(n=3), permission_mode="wrong_site")
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        with httpx.Client(trust_env=False) as client:
            result = run_crawl(
                client, scope, server.origin + "/catalogue/", server.origin + "/PERMISSION.md",
                server.origin + "/robots.txt", server.origin + "/_meta/snapshot",
                CrawlConfig(min_interval_s=0.02, jitter_s=0.0),
            )
    assert result.status == "refused"
    assert result.reasons == ["permission_site_mismatch"]


def test_scope_from_origin_rejects_non_loopback_host():
    import pytest
    from catalogue_extract.boundary import BoundaryError, Scope
    with pytest.raises(BoundaryError) as exc:
        Scope.from_origin("http://example.com:80", ["/catalogue/"])
    assert exc.value.reason == "non_loopback_origin"
