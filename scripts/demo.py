"""End-to-end demo driver: run two crawls against an in-process fixture,
with a deliberate catalogue change in between, then diff and export.

Everything runs in a single process — the fixture server is a
background thread inside this same script, not a separate process — so
there is nothing to poll for and nothing left running afterwards.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx

from catalogue_extract.boundary import Scope
from catalogue_extract.crawl import CrawlConfig, run_crawl
from catalogue_extract.diff import compute_diff
from catalogue_extract.export import write_accounting_report, write_products_csv, write_products_xlsx
from catalogue_extract.snapshot import load_current_id, load_generation, publish
from catalogue_fixture.data import FixtureProduct, generate_products
from catalogue_fixture.site import FixtureServer, FixtureState


def crawl_once(server: FixtureServer, scope: Scope, out_dir: Path):
    with httpx.Client(trust_env=False) as client:
        result = run_crawl(
            client, scope,
            seed_listing_url=server.origin + "/catalogue/",
            permission_url=server.origin + "/PERMISSION.md",
            robots_url=server.origin + "/robots.txt",
            snapshot_meta_url=server.origin + "/_meta/snapshot",
            config=CrawlConfig(min_interval_s=0.3, jitter_s=0.1),
        )
    publish(out_dir, result)
    return result


def main() -> int:
    out_dir = Path(__file__).resolve().parent.parent / "data" / "demo_run"
    out_dir.mkdir(parents=True, exist_ok=True)

    products = generate_products(n=20)
    state = FixtureState(products=products, page_size=7)

    with FixtureServer(state) as server:
        scope = Scope.from_origin(server.origin, ["/catalogue"])

        print(f"[1/4] crawling {server.origin} (baseline) ...")
        first = crawl_once(server, scope, out_dir)
        print(f"      status={first.status} products={len(first.products)}")

        print("[2/4] mutating the fixture catalogue (price change, add, remove) ...")
        mutated = list(products[1:])  # drop products[0] -> a removal
        mutated.append(FixtureProduct("SKU-NEW1", "New Arrival", 4999, "USD", "Gadgets"))  # an addition
        repriced = mutated[0]
        mutated[0] = FixtureProduct(repriced.id, repriced.name, repriced.amount_minor + 500, repriced.currency, repriced.category)
        state.products = mutated
        state.catalogue_version += 1
        state.snapshot_id = f"snap-{state.catalogue_version:04d}"

        print(f"[3/4] crawling {server.origin} (after change) ...")
        second = crawl_once(server, scope, out_dir)
        print(f"      status={second.status} products={len(second.products)}")

    old_gen = load_generation(out_dir, first.crawl_id)
    new_gen = load_generation(out_dir, second.crawl_id)
    diff = compute_diff(old_gen, new_gen)
    print("[4/4] change report:")
    print(json.dumps({k: v for k, v in diff.items() if k != "unchanged"}, indent=2))
    print(f"      unchanged: {len(diff.get('unchanged', []))} products")

    exports_dir = out_dir / "exports"
    exports_dir.mkdir(exist_ok=True)
    write_products_csv(new_gen, exports_dir / "products.csv")
    write_products_xlsx(new_gen, exports_dir / "products.xlsx")
    write_accounting_report(new_gen, exports_dir / "accounting.json")
    (out_dir / "diff.json").write_text(json.dumps(diff, indent=2, sort_keys=True))

    print()
    print(f"current pointer: {load_current_id(out_dir)}")
    print(f"exports written to: {exports_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
