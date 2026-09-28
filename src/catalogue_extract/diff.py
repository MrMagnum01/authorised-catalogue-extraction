"""Change monitoring between two generations.

Added/removed/price-changed/unchanged is only ever claimed between two
*complete* generations of the same scope. Anything less (a failed or
incomplete crawl on either side, or none yet) is reported as
INDETERMINATE with the reason, and the caller keeps whichever complete
baseline it already had — a diff never manufactures a false removal
from a single detail 404.
"""
from __future__ import annotations

from typing import Optional


def compute_diff(old_gen: Optional[dict], new_gen: dict) -> dict:
    if old_gen is None:
        return {"status": "baseline_established", "diagnostics": ["no prior complete generation"]}

    diagnostics = []
    if old_gen.get("scope_origin") != new_gen.get("scope_origin"):
        diagnostics.append("scope_origin_mismatch")
    if old_gen.get("status") != "complete":
        diagnostics.append(f"old_generation_status_{old_gen.get('status')}")
    if new_gen.get("status") != "complete":
        diagnostics.append(f"new_generation_status_{new_gen.get('status')}")

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
