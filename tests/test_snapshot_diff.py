from pathlib import Path

import httpx

from catalogue_extract.boundary import Scope
from catalogue_extract.crawl import CrawlConfig, run_crawl
from catalogue_extract.diff import compute_diff
from catalogue_extract.snapshot import load_current, load_current_id, load_generation, publish
from catalogue_fixture.data import FixtureProduct
from catalogue_fixture.site import FixtureServer, FixtureState


def _client():
    return httpx.Client(trust_env=False)


def _crawl(server, scope, config=None):
    with _client() as client:
        return run_crawl(
            client, scope,
            seed_listing_url=server.origin + "/catalogue/",
            permission_url=server.origin + "/PERMISSION.md",
            robots_url=server.origin + "/robots.txt",
            snapshot_meta_url=server.origin + "/_meta/snapshot",
            config=config or CrawlConfig(min_interval_s=0.02, jitter_s=0.0),
        )


def test_added_removed_price_changed_unchanged(tmp_path: Path):
    a = FixtureProduct("SKU-A", "Alpha", 1000, "USD", "Widgets")
    b = FixtureProduct("SKU-B", "Beta", 2000, "USD", "Widgets")
    c = FixtureProduct("SKU-C", "Gamma", 3000, "USD", "Widgets")
    state = FixtureState(products=[a, b, c], snapshot_id="snap-1", catalogue_version=1)
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        gen1 = _crawl(server, scope)
        publish(tmp_path, gen1)

        d = FixtureProduct("SKU-D", "Delta", 4000, "USD", "Widgets")
        a_repriced = FixtureProduct("SKU-A", "Alpha", 1500, "USD", "Widgets")
        state.products = [a_repriced, b, d]
        state.snapshot_id = "snap-2"
        state.catalogue_version = 2
        gen2 = _crawl(server, scope)
        publish(tmp_path, gen2)

    assert gen1.status == "complete"
    assert gen2.status == "complete"
    old = load_generation(tmp_path, gen1.crawl_id)
    new = load_generation(tmp_path, gen2.crawl_id)
    result = compute_diff(old, new)
    assert result["status"] == "complete"
    assert result["added"] == ["SKU-D"]
    assert result["removed"] == ["SKU-C"]
    assert result["price_changed"] == ["SKU-A"]
    assert result["unchanged"] == ["SKU-B"]
    assert load_current_id(tmp_path) == gen2.crawl_id


def test_currency_change_is_anomaly_not_price_change(tmp_path: Path):
    a = FixtureProduct("SKU-A", "Alpha", 1000, "USD", "Widgets")
    state = FixtureState(products=[a], snapshot_id="s1", catalogue_version=1)
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        gen1 = _crawl(server, scope)
        publish(tmp_path, gen1)

        state.products = [FixtureProduct("SKU-A", "Alpha", 1000, "EUR", "Widgets")]
        state.snapshot_id = "s2"
        state.catalogue_version = 2
        gen2 = _crawl(server, scope)
        publish(tmp_path, gen2)

    result = compute_diff(load_generation(tmp_path, gen1.crawl_id), load_generation(tmp_path, gen2.crawl_id))
    assert result["currency_changed_anomaly"] == ["SKU-A"]
    assert result["price_changed"] == []


def test_valid_empty_catalogue_is_a_complete_baseline(tmp_path: Path):
    state = FixtureState(products=[])
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        gen = _crawl(server, scope)
        publish(tmp_path, gen)
    assert gen.status == "complete"
    assert gen.products == []
    assert load_current(tmp_path)["crawl_id"] == gen.crawl_id
    result = compute_diff(None, load_generation(tmp_path, gen.crawl_id))
    assert result["status"] == "baseline_established"


def test_mid_crawl_snapshot_change_is_indeterminate_and_keeps_baseline(tmp_path: Path):
    products = [FixtureProduct(f"SKU-{i}", f"Product {i}", 1000 + i, "USD", "Widgets") for i in range(5)]
    state = FixtureState(products=products, page_size=1)
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        good = _crawl(server, scope)
        publish(tmp_path, good)
        assert good.status == "complete"
        baseline_id = load_current_id(tmp_path)

        # Force the catalogue version to bump partway through the next crawl.
        state.request_count = 0
        state.bump_version_after = 2
        interrupted = _crawl(server, scope)
        publish(tmp_path, interrupted)

    assert interrupted.status == "incomplete"
    assert "snapshot_changed_mid_crawl" in interrupted.reasons
    assert load_current_id(tmp_path) == baseline_id  # unchanged by the incomplete crawl

    result = compute_diff(load_current(tmp_path), load_generation(tmp_path, interrupted.crawl_id))
    assert result["status"] == "INDETERMINATE"
    assert result["baseline_crawl_id"] == baseline_id


def test_failed_crawl_then_successful_retry_only_second_becomes_current(tmp_path: Path):
    state = FixtureState(products=[FixtureProduct("SKU-A", "Alpha", 1000, "USD", "Widgets")], robots_mode="error500")
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        failed = _crawl(server, scope)
        publish(tmp_path, failed)
        assert failed.status == "refused"
        assert load_current_id(tmp_path) is None  # a refused run never becomes current

        state.robots_mode = "normal"
        ok = _crawl(server, scope)
        publish(tmp_path, ok)

    assert ok.status == "complete"
    assert load_current_id(tmp_path) == ok.crawl_id
    # The failed generation is retained for audit, never deleted.
    assert (tmp_path / "generations" / failed.crawl_id / "generation.json").exists()
