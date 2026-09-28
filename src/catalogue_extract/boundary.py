"""Pre-request scope enforcement.

Every candidate URL — the seed, a pagination link, a detail link, or a
redirect Location — passes through here before any socket opens. The
policy is intentionally stricter than "same host": one exact scheme,
host and port, plus an explicit allow-list of path prefixes. Nothing
about localhost is special-cased; a second loopback service on a
different port is out of scope exactly like a public third-party host.
"""
from __future__ import annotations

import ipaddress
import posixpath
from dataclasses import dataclass
from urllib.parse import unquote, urlsplit

DEFAULT_PORTS = {"http": 80, "https": 443}


class BoundaryError(ValueError):
    def __init__(self, reason: str, url: str):
        super().__init__(f"{reason}: {url}")
        self.reason = reason
        self.url = url


@dataclass(frozen=True)
class Scope:
    scheme: str
    host: str
    port: int
    allowed_prefixes: tuple[str, ...]

    @property
    def origin(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"

    @classmethod
    def from_origin(cls, origin: str, allowed_prefixes: list[str]) -> "Scope":
        parts = urlsplit(origin)
        if not parts.scheme or not parts.hostname or parts.username or parts.password:
            raise BoundaryError("malformed_origin", origin)
        port = parts.port or DEFAULT_PORTS.get(parts.scheme)
        if port is None:
            raise BoundaryError("malformed_origin", origin)
        try:
            is_loopback = ipaddress.ip_address(parts.hostname).is_loopback
        except ValueError:
            is_loopback = False
        if not is_loopback:
            raise BoundaryError("non_loopback_origin", origin)
        prefixes = tuple(p if p.startswith("/") else f"/{p}" for p in allowed_prefixes)
        if not prefixes:
            raise BoundaryError("no_allowed_prefixes", origin)
        return cls(scheme=parts.scheme, host=parts.hostname.lower(), port=port, allowed_prefixes=prefixes)


def _normalize_path(raw_path: str) -> str:
    """Percent-decode then collapse dot-segments, catching encoded traversal."""
    decoded = unquote(raw_path)
    if "\x00" in decoded:
        raise BoundaryError("malformed_path", raw_path)
    normalized = posixpath.normpath(decoded)
    if normalized == ".":
        normalized = "/"
    if not normalized.startswith("/"):
        normalized = "/" + normalized
    return normalized


def check_in_scope(url: str, scope: Scope, *, require_prefix: bool = True) -> str:
    """Return the normalized in-scope path, or raise BoundaryError.

    `require_prefix=False` validates the exact scheme/host/port (and
    rejects credentials, malformed URLs and traversal) without requiring
    the path to fall under `scope.allowed_prefixes`. Used for the fixed
    control endpoints (PERMISSION.md, robots.txt, the snapshot-meta
    endpoint), which are allowed to live outside the crawl's own allowed
    paths but must still be the exact, single authorised origin — not a
    same-prefix string match, which a port like `:12345` can spoof
    against an allowed `:1234`.
    """
    parts = urlsplit(url)
    if parts.username is not None or parts.password is not None:
        raise BoundaryError("credentials_in_url", url)
    if not parts.scheme:
        raise BoundaryError("malformed_url", url)
    if parts.scheme != scope.scheme:
        raise BoundaryError("scheme_mismatch", url)
    if not parts.hostname or parts.hostname.lower() != scope.host:
        raise BoundaryError("host_mismatch", url)
    port = parts.port or DEFAULT_PORTS.get(parts.scheme)
    if port != scope.port:
        raise BoundaryError("port_mismatch", url)
    normalized = _normalize_path(parts.path or "/")
    if normalized.startswith("/..") or normalized == "..":
        raise BoundaryError("path_traversal", url)
    if not require_prefix:
        return normalized
    if not any(normalized == p or normalized.startswith(p.rstrip("/") + "/") or normalized == p.rstrip("/")
               for p in scope.allowed_prefixes):
        raise BoundaryError("path_out_of_scope", url)
    return normalized


def is_in_scope(url: str, scope: Scope, *, require_prefix: bool = True) -> bool:
    try:
        check_in_scope(url, scope, require_prefix=require_prefix)
        return True
    except BoundaryError:
        return False


def canonical_url(url: str) -> str:
    """Canonical key for dedup: scheme, host, port, normalized path, sorted query."""
    parts = urlsplit(url)
    port = parts.port or DEFAULT_PORTS.get(parts.scheme, 0)
    path = _normalize_path(parts.path or "/")
    query_pairs = sorted(
        pair.split("=", 1) if "=" in pair else (pair, "")
        for pair in parts.query.split("&") if pair
    )
    query = "&".join(f"{k}={v}" for k, v in query_pairs)
    return f"{parts.scheme}://{(parts.hostname or '').lower()}:{port}{path}?{query}"
