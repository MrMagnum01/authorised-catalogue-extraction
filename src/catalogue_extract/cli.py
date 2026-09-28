from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

from .boundary import Scope
from .crawl import CrawlConfig, run_crawl
from .diff import compute_diff
from .export import write_accounting_report, write_products_csv, write_products_xlsx
from .snapshot import load_current, load_generation, publish


def build_client() -> httpx.Client:
    return httpx.Client(trust_env=False)


def cmd_crawl(args: argparse.Namespace) -> int:
    scope = Scope.from_origin(args.origin, args.allow)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    config = CrawlConfig(max_page_count=args.max_pages)
    with build_client() as client:
        result = run_crawl(
            client, scope,
            seed_listing_url=args.origin + args.seed_path,
            permission_url=args.origin + args.permission_path,
            robots_url=args.origin + "/robots.txt",
            snapshot_meta_url=args.origin + args.snapshot_path,
            config=config,
        )
    gen_path = publish(out_dir, result)
    print(f"crawl_id={result.crawl_id} status={result.status} reasons={result.reasons}")
    print(f"generation={gen_path}")
    return 0 if result.status != "refused" else 2


def cmd_diff(args: argparse.Namespace) -> int:
    out_dir = Path(args.dir)
    old_gen = load_generation(out_dir, args.old) if args.old else None
    new_gen = load_generation(out_dir, args.new)
    result = compute_diff(old_gen, new_gen)
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.write_to:
        Path(args.write_to).write_text(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] in ("complete", "baseline_established") else 3


def cmd_export(args: argparse.Namespace) -> int:
    out_dir = Path(args.dir)
    generation = load_generation(out_dir, args.crawl_id)
    dest = Path(args.out)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "csv":
        write_products_csv(generation, dest)
    elif args.format == "xlsx":
        write_products_xlsx(generation, dest)
    else:
        write_accounting_report(generation, dest)
    print(f"wrote {dest}")
    return 0


def cmd_current(args: argparse.Namespace) -> int:
    out_dir = Path(args.dir)
    current = load_current(out_dir)
    if current is None:
        print("no complete generation yet")
        return 1
    print(json.dumps({"crawl_id": current["crawl_id"], "status": current["status"]}, indent=2))
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="catalogue-extract")
    sub = parser.add_subparsers(dest="command", required=True)

    p_crawl = sub.add_parser("crawl")
    p_crawl.add_argument("--origin", required=True)
    p_crawl.add_argument("--allow", nargs="+", required=True)
    p_crawl.add_argument("--seed-path", default="/catalogue/")
    p_crawl.add_argument("--permission-path", default="/PERMISSION.md")
    p_crawl.add_argument("--snapshot-path", default="/_meta/snapshot")
    p_crawl.add_argument("--out", required=True)
    p_crawl.add_argument("--max-pages", type=int, default=200)
    p_crawl.set_defaults(func=cmd_crawl)

    p_diff = sub.add_parser("diff")
    p_diff.add_argument("--dir", required=True)
    p_diff.add_argument("--old")
    p_diff.add_argument("--new", required=True)
    p_diff.add_argument("--write-to")
    p_diff.set_defaults(func=cmd_diff)

    p_export = sub.add_parser("export")
    p_export.add_argument("--dir", required=True)
    p_export.add_argument("--crawl-id", required=True)
    p_export.add_argument("--format", choices=["csv", "xlsx", "report"], required=True)
    p_export.add_argument("--out", required=True)
    p_export.set_defaults(func=cmd_export)

    p_current = sub.add_parser("current")
    p_current.add_argument("--dir", required=True)
    p_current.set_defaults(func=cmd_current)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
