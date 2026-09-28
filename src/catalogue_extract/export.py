"""CSV/XLSX export with the project's formula-injection mitigation.

Mitigation: any exported text field starting with `=`, `+`, `-`, `@`,
tab or carriage-return is prefixed with a leading single quote. This is
the conventional Excel/Sheets "treat as text" marker on *import*; it is
not a guarantee that every CSV consumer honours it, and plain CSV has
no native escaping for spreadsheet formulas — hence "the chosen
mitigation", not a universal-safety claim. The unmodified value is
never lost: it stays in the JSON generation file untouched, only the
CSV/XLSX display copy is transformed.
"""
from __future__ import annotations

import csv
import json
from pathlib import Path

from openpyxl import Workbook

from .boundary import canonical_url

_DANGEROUS_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

CSV_COLUMNS = [
    "product_id", "name", "amount_minor", "currency", "category", "template",
    "source_url", "final_url", "fetch_utc", "http_status", "content_sha256", "parser_version",
]


def sanitize_formula_field(value):
    if value is None:
        return value
    text = str(value)
    if text and text[0] in _DANGEROUS_PREFIXES:
        return "'" + text
    return text


def products_with_provenance(generation: dict) -> list[dict]:
    pages = generation.get("pages", {})
    rows = []
    for p in generation["products"]:
        source_url = p["source_urls"][0]
        page = pages.get(canonical_url(source_url), {})
        rows.append({
            "product_id": p["product_id"],
            "name": p["name"],
            "amount_minor": p["price"]["amount_minor"] if p["price"] else None,
            "currency": p["price"]["currency"] if p["price"] else None,
            "category": p["category"],
            "template": p["template"],
            "source_url": source_url,
            "final_url": page.get("final_url"),
            "fetch_utc": page.get("fetch_utc"),
            "http_status": page.get("status_code"),
            "content_sha256": page.get("content_sha256"),
            "parser_version": p["parser_version"],
        })
    return rows


def write_products_csv(generation: dict, path: Path) -> None:
    rows = products_with_provenance(generation)
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            safe_row = dict(row)
            safe_row["name"] = sanitize_formula_field(row["name"])
            safe_row["category"] = sanitize_formula_field(row["category"])
            writer.writerow(safe_row)


def write_products_xlsx(generation: dict, path: Path) -> None:
    rows = products_with_provenance(generation)
    wb = Workbook()
    ws = wb.active
    ws.title = "products"
    ws.append(CSV_COLUMNS)
    for row in rows:
        ws.append([
            row["product_id"],
            sanitize_formula_field(row["name"]),
            row["amount_minor"],
            row["currency"],
            sanitize_formula_field(row["category"]),
            row["template"],
            row["source_url"],
            row["final_url"],
            row["fetch_utc"],
            row["http_status"],
            row["content_sha256"],
            row["parser_version"],
        ])
    wb.save(path)


def write_accounting_report(generation: dict, path: Path) -> None:
    path.write_text(json.dumps({
        "crawl_id": generation["crawl_id"],
        "status": generation["status"],
        "reasons": generation["reasons"],
        "accounting": generation["accounting"],
        "expected_items": generation["expected_items"],
        "delivered_accepted": len(generation["products"]),
        "delivered_exact_duplicates": generation["exact_duplicates"],
        "delivered_rejected": len(generation["rejected"]),
        "delivered_conflicting_ids": len(generation["conflicting_ids"]),
        "out_of_scope_link_count": len(generation["out_of_scope_urls"]),
        "listing_detail_overlap": generation["listing_detail_overlap"],
    }, indent=2, sort_keys=True))
