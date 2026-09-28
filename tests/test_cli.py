import json
from pathlib import Path

from catalogue_extract.cli import main
from catalogue_fixture.data import generate_products
from catalogue_fixture.site import FixtureServer, FixtureState


def test_cli_crawl_export_and_diff_roundtrip(tmp_path: Path):
    state = FixtureState(products=generate_products(n=6), page_size=3)
    with FixtureServer(state) as server:
        out_dir = tmp_path / "run"
        rc = main([
            "crawl", "--origin", server.origin, "--allow", "/catalogue",
            "--out", str(out_dir),
        ])
        assert rc == 0

        current_id = (out_dir / "current").read_text().strip()

        state.products = state.products[:-1]  # remove the last product
        rc2 = main([
            "crawl", "--origin", server.origin, "--allow", "/catalogue",
            "--out", str(out_dir),
        ])
        assert rc2 == 0
        new_id = (out_dir / "current").read_text().strip()
        assert new_id != current_id

    diff_path = tmp_path / "diff.json"
    rc3 = main([
        "diff", "--dir", str(out_dir), "--old", current_id, "--new", new_id,
        "--write-to", str(diff_path),
    ])
    assert rc3 == 0
    diff = json.loads(diff_path.read_text())
    assert diff["status"] == "complete"
    assert len(diff["removed"]) == 1

    csv_path = tmp_path / "products.csv"
    rc4 = main([
        "export", "--dir", str(out_dir), "--crawl-id", new_id,
        "--format", "csv", "--out", str(csv_path),
    ])
    assert rc4 == 0
    assert csv_path.exists()
    assert "product_id" in csv_path.read_text().splitlines()[0]


def test_cli_diff_returns_nonzero_for_indeterminate_result(tmp_path: Path):
    """Astra HOLD group 5: automation must be able to tell an INDETERMINATE
    diff apart from a real one by exit code alone, not just by re-parsing
    the JSON it printed."""
    state = FixtureState(products=generate_products(n=2), robots_mode="error500")
    with FixtureServer(state) as server:
        out_dir = tmp_path / "run"
        rc = main(["crawl", "--origin", server.origin, "--allow", "/catalogue", "--out", str(out_dir)])
        assert rc == 2  # refused

    refused_id = None
    for p in (out_dir / "generations").iterdir():
        refused_id = p.name
    assert refused_id is not None

    diff_path = tmp_path / "diff.json"
    rc2 = main(["diff", "--dir", str(out_dir), "--new", refused_id, "--write-to", str(diff_path)])
    assert rc2 != 0
    diff = json.loads(diff_path.read_text())
    assert diff["status"] == "INDETERMINATE"
