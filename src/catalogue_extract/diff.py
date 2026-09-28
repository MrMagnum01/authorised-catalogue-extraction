"""Change monitoring between two generations.

Added/removed/price-changed/unchanged is only ever claimed between two
*complete* generations of the same authorised scope — same origin,
same allowed paths, same seed URL and the same permission grant
(compared by its hash). A new generation's own completeness is checked
before anything else, including the very first crawl: a `refused` or
`incomplete` first run never becomes a `baseline_established` result,
only a `complete` one does. Anything less than that is reported as
INDETERMINATE with the reason, and the caller keeps whichever complete
baseline it already had — a diff never manufactures a false removal
from a single detail 404, and never claims removals across two
generations that scoped or authorised a different subset of the site.

"unchanged" narrows specifically to *price and currency* being
identical between the two snapshots — name/category drift on the same
product ID is not currently compared or reported here.
"""
from __future__ import annotations

from typing import Optional


def compute_diff(old_gen: Optional[dict], new_gen: dict) -> dict:
    diagnostics: list[str] = []
    if new_gen.get("status") != "complete":
        diagnostics.append(f"new_generation_status_{new_gen.get('status')}")

    if old_gen is None:
        if diagnostics:
            return {"status": "INDETERMINATE", "diagnostics": diagnostics, "baseline_crawl_id": None}
        return {"status": "baseline_established", "diagnostics": ["no prior complete generation"]}

    if old_gen.get("status") != "complete":
        diagnostics.append(f"old_generation_status_{old_gen.get('status')}")
    if old_gen.get("scope_origin") != new_gen.get("scope_origin"):
        diagnostics.append("scope_origin_mismatch")
    if old_gen.get("allowed_prefixes") != new_gen.get("allowed_prefixes"):
        diagnostics.append("allowed_paths_mismatch")
    if old_gen.get("seed_listing_url") != new_gen.get("seed_listing_url"):
        diagnostics.append("seed_url_mismatch")
    if old_gen.get("permission_sha256") != new_gen.get("permission_sha256"):
        diagnostics.append("permission_identity_mismatch")

    if diagnostics:
        return {"status": "INDETERMINATE", "diagnostics": diagnostics, "baseline_crawl_id": old_gen.get("crawl_id")}

    old_products = {p["product_id"]: p for p in old_gen["products"]}
    new_products = {p["product_id"]: p for p in new_gen["products"]}

    added = sorted(set(new_products) - set(old_products))
    removed = sorted(set(old_products) - set(new_products))
    price_changed, unchanged, currency_changed = [], [], []

    for pid in sorted(set(old_products) & set(new_products)):
        old_price = old_products[pid]["price"]
        new_price = new_products[pid]["price"]
        if old_price["currency"] != new_price["currency"]:
            currency_changed.append(pid)
        elif old_price["amount_minor"] != new_price["amount_minor"]:
            price_changed.append(pid)
        else:
            unchanged.append(pid)

    return {
        "status": "complete",
        "old_crawl_id": old_gen["crawl_id"],
        "new_crawl_id": new_gen["crawl_id"],
        "added": added,
        "removed": removed,
        "price_changed": price_changed,
        "unchanged": unchanged,
        "currency_changed_anomaly": currency_changed,
    }
