"""PERMISSION.md grammar: a validated grant, not just a hash of arbitrary text.

robots.txt is a crawl-policy signal (see `robots.py`); PERMISSION.md is
the legal authorisation this demo requires before crawling at all. A
200 response is not itself a grant — the body must parse as this exact,
minimal grammar and explicitly say `Permission: granted` for this exact
origin, or the run is refused. The CLI's `--allow` prefixes are then
checked against the grant's own `Allowed-Paths`: the command line can
narrow what a run touches, never widen it past what the target site
actually granted.

Grammar (one `Key: value` pair per line; blank lines and `#` comments
ignored; keys are case-sensitive and each required exactly once):

    Site: <exact scheme://host:port, matched against the crawl's Scope>
    Allowed-Paths: <comma-separated path prefixes, each starting with />
    Purpose: <free text, non-empty>
    Permission: granted

Anything else — a missing key, an empty value, a required key repeated
(whether the repeat agrees or conflicts — a grant that says both
`Permission: granted` and `Permission: denied` is not a clean grant,
even if the first line is read as authoritative), a malformed
`Allowed-Paths` entry not starting with `/`, `Permission: denied` (or
any value other than `granted`), or a `Site` that doesn't match the
exact origin being crawled — fails validation. This is this demo's own
grammar, invented for this repository; it is not a claim about any
real-world permission-file standard.
"""
from __future__ import annotations

from dataclasses import dataclass

from .boundary import Scope

REQUIRED_KEYS = ("Site", "Allowed-Paths", "Purpose", "Permission")


@dataclass(frozen=True)
class PermissionGrant:
    ok: bool
    reason: str | None
    site: str | None = None
    allowed_paths: tuple[str, ...] = ()
    purpose: str | None = None


def parse_permission(text: str) -> PermissionGrant:
    fields: dict[str, str] = {}
    duplicate_keys: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key in REQUIRED_KEYS:
            if key in fields:
                duplicate_keys.add(key)
            else:
                fields[key] = value.strip()

    if duplicate_keys:
        return PermissionGrant(False, f"duplicate_field:{'+'.join(sorted(duplicate_keys))}")

    missing = [k for k in REQUIRED_KEYS if not fields.get(k)]
    if missing:
        return PermissionGrant(False, f"missing_field:{'+'.join(missing)}")

    if fields["Permission"].lower() != "granted":
        return PermissionGrant(False, "not_granted")

    raw_paths = [p.strip() for p in fields["Allowed-Paths"].split(",") if p.strip()]
    if not raw_paths:
        return PermissionGrant(False, "missing_field:Allowed-Paths")
    if any(not p.startswith("/") for p in raw_paths):
        return PermissionGrant(False, "malformed_allowed_path")
    allowed_paths = tuple(raw_paths)

    return PermissionGrant(
        ok=True, reason=None,
        site=fields["Site"], allowed_paths=allowed_paths, purpose=fields["Purpose"],
    )


def validate_permission_scope(grant: PermissionGrant, scope: Scope) -> str | None:
    """Return a refusal reason, or None if `scope` is fully covered by `grant`."""
    if grant.site != scope.origin:
        return "permission_site_mismatch"
    for requested in scope.allowed_prefixes:
        covered = any(
            requested == granted or requested.rstrip("/").startswith(granted.rstrip("/") + "/")
            or requested.rstrip("/") == granted.rstrip("/")
            for granted in grant.allowed_paths
        )
        if not covered:
            return "cli_scope_exceeds_permission"
    return None
