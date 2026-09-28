"""Deterministic synthetic catalogue + its ground-truth ledger.

This is the single source of truth for what the fixture serves. Tests
compare crawl output against these plain Python objects directly —
never against anything the extractor itself produced — so the ledger
is independent of the extraction logic it's used to check.
"""
from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass
class FixtureProduct:
    id: str
    name: str
    amount_minor: int
    currency: str
    category: str


def generate_products(n: int = 23, seed: int = 42) -> list[FixtureProduct]:
    rng = random.Random(seed)
    categories = ["Widgets", "Gadgets", "Tools", "Accessories"]
    products = []
    for i in range(1, n + 1):
        products.append(FixtureProduct(
            id=f"SKU-{i:04d}",
            name=f"Product {i:04d}",
            amount_minor=rng.randint(500, 49999),
            currency="USD",
            category=categories[i % len(categories)],
        ))
    return products
