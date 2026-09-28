"""Generation storage: one JSON file per crawl, an atomic `current` pointer.

Every crawl — complete, incomplete or refused — is written to its own
generation file and is never deleted by this code. Only a *complete*
crawl ever becomes the `current` pointer, via write-temp-then-rename
(atomic on the same filesystem), so a reader never observes a half
written pointer and a failed/incomplete crawl never displaces a good
baseline. This module makes no promise about generations held open by
readers across a GC pass, because it never runs one.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path
from typing import Optional

from .crawl import CrawlResult
from .models import Money


def _money_to_dict(m: Optional[Money]) -> Optional[dict]:
    if m is None:
        return None
    return {"amount_minor": m.amount_minor, "currency": m.currency}


def crawl_result_to_dict(result: CrawlResult) -> dict:
    return {
        "crawl_id": result.crawl_id,
        "status": result.status,
        "reasons": result.reasons,
        "started_at": result.started_at,
        "ended_at": result.ended_at,
        "scope_origin": result.scope_origin,
        "permission_sha256": result.permission_sha256,
        "permission_fetched_at": result.permission_fetched_at,
        "robots": None if result.robots is None else {
            "ok": result.robots.ok,
            "reason": result.robots.reason,
            "status_code": result.robots.status_code,
            "fetched_at": result.robots.fetched_at,
            "sha256": result.robots.sha256,
            "byte_count": result.robots.byte_count,
        },
        "start_snapshot": result.start_snapshot,
        "end_snapshot": result.end_snapshot,
        "listing_detail_overlap": result.listing_detail_overlap,
        "accounting": asdict(result.accounting),
        "expected_items": result.expected_items,
        "exact_duplicates": result.exact_duplicates,
        "out_of_scope_urls": sorted(result.out_of_scope_urls),
        "pages": {
            canon: {
                "url": p.url, "kind": p.kind, "outcome": p.outcome,
                "final_url": p.final_url, "status_code": p.status_code,
                "fetch_utc": p.fetch_utc, "content_sha256": p.content_sha256,
                "reason": p.reason,
                "attempts": [
                    {"attempt_no": a.attempt_no, "url": a.url, "status_code": a.status_code,
                     "error": a.error, "elapsed_s": a.elapsed_s, "waited_before_s": a.waited_before_s}
                    for a in p.attempts
                ],
            }
            for canon, p in result.pages.items()
        },
        "products": [
            {
                "product_id": pr.product_id, "name": pr.name, "price": _money_to_dict(pr.price),
                "price_status": pr.price_status, "category": pr.category, "template": pr.template,
                "source_urls": pr.source_urls, "content_hash": pr.content_hash,
                "parser_version": pr.parser_version,
            }
            for pr in result.products
        ],
        "rejected": [
            {
                "product_id": r.product_id, "name": r.name, "price": _money_to_dict(r.price),
                "price_status": r.price_status, "category": r.category, "template": r.template,
                "source_url": r.source_url, "content_hash": r.content_hash,
                "outcome": r.outcome, "reason": r.reason,
            }
            for r in result.rejected
        ],
        "conflicting_ids": {
            pid: [
                {
                    "product_id": r.product_id, "name": r.name, "price": _money_to_dict(r.price),
                    "price_status": r.price_status, "category": r.category, "template": r.template,
                    "source_url": r.source_url, "content_hash": r.content_hash,
                }
                for r in records
            ]
            for pid, records in result.conflicting_ids.items()
        },
    }


def save_generation(out_dir: Path, result: CrawlResult) -> Path:
    gen_dir = out_dir / "generations" / result.crawl_id
    gen_dir.mkdir(parents=True, exist_ok=True)
    gen_path = gen_dir / "generation.json"
    tmp_path = gen_path.with_suffix(".json.tmp")
    tmp_path.write_text(json.dumps(crawl_result_to_dict(result), indent=2, sort_keys=True))
    os.replace(tmp_path, gen_path)
    return gen_path


def update_current_pointer(out_dir: Path, crawl_id: str) -> None:
    pointer_path = out_dir / "current"
    tmp_path = out_dir / "current.tmp"
    tmp_path.write_text(crawl_id)
    os.replace(tmp_path, pointer_path)


def load_current_id(out_dir: Path) -> Optional[str]:
    pointer_path = out_dir / "current"
    if not pointer_path.exists():
        return None
    return pointer_path.read_text().strip()


def load_generation(out_dir: Path, crawl_id: str) -> dict:
    gen_path = out_dir / "generations" / crawl_id / "generation.json"
    return json.loads(gen_path.read_text())


def load_current(out_dir: Path) -> Optional[dict]:
    crawl_id = load_current_id(out_dir)
    if crawl_id is None:
        return None
    return load_generation(out_dir, crawl_id)


def publish(out_dir: Path, result: CrawlResult) -> Path:
    """Save the generation; advance `current` only if it is complete."""
    gen_path = save_generation(out_dir, result)
    if result.status == "complete":
        update_current_pointer(out_dir, result.crawl_id)
    return gen_path
