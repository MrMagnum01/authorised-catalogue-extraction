import httpx

from catalogue_extract.robots import can_fetch, fetch_and_parse_robots
from catalogue_fixture.data import generate_products
from catalogue_fixture.site import FixtureServer, FixtureState


def _client():
    return httpx.Client(trust_env=False)


def test_normal_robots_parses_and_denies_disallowed_path():
    state = FixtureState(products=generate_products(n=3), robots_disallow=("/catalogue/product/blocked",))
    with FixtureServer(state) as server, _client() as client:
        result = fetch_and_parse_robots(client, server.origin + "/robots.txt")
        assert result.ok
        assert result.sha256 is not None
        assert can_fetch(result, server.origin + "/catalogue/", "demo-agent") is True
        assert can_fetch(result, server.origin + "/catalogue/product/blocked-1", "demo-agent") is False


def test_missing_robots_refuses():
    state = FixtureState(robots_mode="missing")
    with FixtureServer(state) as server, _client() as client:
        result = fetch_and_parse_robots(client, server.origin + "/robots.txt")
        assert not result.ok
        assert result.reason == "missing"


def test_server_error_robots_refuses():
    state = FixtureState(robots_mode="error500")
    with FixtureServer(state) as server, _client() as client:
        result = fetch_and_parse_robots(client, server.origin + "/robots.txt")
        assert not result.ok
        assert result.reason == "server_error"


def test_rate_limited_robots_refuses():
    state = FixtureState(robots_mode="ratelimited")
    with FixtureServer(state) as server, _client() as client:
        result = fetch_and_parse_robots(client, server.origin + "/robots.txt")
        assert not result.ok
        assert result.reason == "rate_limited"


def test_malformed_robots_refuses():
    state = FixtureState(robots_mode="malformed")
    with FixtureServer(state) as server, _client() as client:
        result = fetch_and_parse_robots(client, server.origin + "/robots.txt")
        assert not result.ok
        assert result.reason == "malformed_bytes"


def test_empty_catalogue_with_normal_robots_is_allowed_not_an_error():
    state = FixtureState(products=[])
    with FixtureServer(state) as server, _client() as client:
        result = fetch_and_parse_robots(client, server.origin + "/robots.txt")
        assert result.ok
        assert can_fetch(result, server.origin + "/catalogue/", "demo-agent") is True
