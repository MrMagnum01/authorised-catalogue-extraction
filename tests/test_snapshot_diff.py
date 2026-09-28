from pathlib import Path

import httpx

from catalogue_extract.boundary import Scope, canonical_url
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


def test_no_baseline_but_refused_new_is_indeterminate_not_baseline():
    """Astra HOLD group 5: `compute_diff` must check the *new* generation's
    completeness before ever returning `baseline_established`, even when
    there is no prior generation at all."""
    result = compute_diff(None, {"status": "refused"})
    assert result["status"] == "INDETERMINATE"
    assert any("new_generation_status_refused" in d for d in result["diagnostics"])


def test_no_baseline_and_complete_new_establishes_baseline():
    result = compute_diff(None, {"status": "complete", "crawl_id": "abc", "products": []})
    assert result["status"] == "baseline_established"


def test_diff_rejects_removals_from_a_narrower_authorised_subset(tmp_path: Path):
    """Astra HOLD group 5: two complete crawls of the same scope_origin
    but different authorised path/seed/permission bindings must not be
    diffed as if they covered the same catalogue subset — a narrower
    second run must not manufacture "removed" products."""
    a = FixtureProduct("SKU-A", "Alpha", 1000, "USD", "Widgets")
    b = FixtureProduct("SKU-B", "Beta", 2000, "USD", "Widgets")
    state = FixtureState(products=[a, b])
    with FixtureServer(state) as server:
        wide_scope = Scope.from_origin(server.origin, ["/catalogue"])
        gen1 = _crawl(server, wide_scope)
        publish(tmp_path, gen1)
        assert gen1.status == "complete"

        # A second, real crawl of the identical scope/seed/permission is a
        # legitimate comparison (sanity: same bindings still diff cleanly).
        gen_same = _crawl(server, wide_scope)
        publish(tmp_path, gen_same)

    old = load_generation(tmp_path, gen1.crawl_id)
    same = load_generation(tmp_path, gen_same.crawl_id)
    assert compute_diff(old, same)["status"] == "complete"

    # Now simulate a generation that was bound to a different seed URL —
    # e.g. a differently-parameterised crawl of the same origin — by
    # mutating the loaded dict directly (this is the exact shape a
    # narrower/parallel authorised subset would produce).
    narrower = dict(same)
    narrower["seed_listing_url"] = same["seed_listing_url"] + "?page=2"
    result = compute_diff(old, narrower)
    assert result["status"] == "INDETERMINATE"
    assert "seed_url_mismatch" in result["diagnostics"]


def test_diff_rejects_mismatched_permission_identity(tmp_path: Path):
    a = FixtureProduct("SKU-A", "Alpha", 1000, "USD", "Widgets")
    state = FixtureState(products=[a])
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        gen1 = _crawl(server, scope)
        publish(tmp_path, gen1)
    old = load_generation(tmp_path, gen1.crawl_id)
    mutated = dict(old)
    mutated["permission_sha256"] = "deadbeef" * 8
    result = compute_diff(old, mutated)
    assert result["status"] == "INDETERMINATE"
    assert "permission_identity_mismatch" in result["diagnostics"]


def test_diff_rejects_mismatched_allowed_prefixes(tmp_path: Path):
    a = FixtureProduct("SKU-A", "Alpha", 1000, "USD", "Widgets")
    state = FixtureState(products=[a])
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        gen1 = _crawl(server, scope)
        publish(tmp_path, gen1)
    old = load_generation(tmp_path, gen1.crawl_id)
    mutated = dict(old)
    mutated["allowed_prefixes"] = ["/catalogue", "/extra"]
    result = compute_diff(old, mutated)
    assert result["status"] == "INDETERMINATE"
    assert "allowed_paths_mismatch" in result["diagnostics"]


def test_raw_response_artifacts_retained_per_fetch(tmp_path: Path):
    """Astra HOLD r2, group7: the generation JSON used to serialize only
    hashes/metadata, with no way to independently verify a fetch's raw
    bytes. Every *page* fetch attempt with an actual HTTP response must
    now also retain a header subset and the raw body, addressable by
    hash, under the generation's own `blobs/` directory (control fetches
    stay represented by their own metadata/hashes only)."""
    a = FixtureProduct("SKU-A", "Alpha", 1000, "USD", "Widgets")
    state = FixtureState(products=[a])
    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])
        result = _crawl(server, scope)
        publish(tmp_path, result)
    assert result.status == "complete"

    generation = load_generation(tmp_path, result.crawl_id)
    seed_page = generation["pages"][canonical_url(server.origin + "/catalogue/")]
    attempt = seed_page["attempts"][0]
    assert attempt["status_code"] == 200
    assert attempt["response_headers"].get("content-type", "").startswith("text/html")
    assert attempt["body_sha256"]

    blob_path = tmp_path / "generations" / result.crawl_id / "blobs" / f"{attempt['body_sha256']}.bin"
    assert blob_path.exists()
    assert blob_path.read_bytes()


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
