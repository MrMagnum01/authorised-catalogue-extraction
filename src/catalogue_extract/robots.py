"""robots.txt fetch + parse.

Permission and robots are kept deliberately separate: PERMISSION.md is
the legal authorisation this demo requires before crawling at all;
robots.txt is a crawl-policy signal layered on top of it, consulted
per-URL. Parsing uses Protego (pinned in requirements.txt), which
implements the tolerant, group-merging semantics described in RFC 9309
(checked 2026-09-28) plus the historical extensions (wildcards, `$`
anchors) most real robots.txt files rely on. We do not implement or
claim support for anything Protego doesn't parse (e.g. Crawl-delay is
ignored — this demo uses its own fixed rate limit instead).

Demo policy (stricter than RFC 9309, by design, not by claim about the
RFC): a missing, unreadable, rate-limited or erroring robots.txt makes
the whole run incomplete/refused. We do not fall back to allow-all.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import httpx
from protego import Protego

PROTEGO_VERSION = "0.7.0"


@dataclass
class RobotsResult:
    ok: bool
    reason: Optional[str]
    status_code: Optional[int]
    fetched_at: str
    sha256: Optional[str]
    byte_count: Optional[int]
    parser: Optional[Protego] = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def fetch_and_parse_robots(client: httpx.Client, robots_url: str) -> RobotsResult:
    fetched_at = _now_iso()
    try:
        resp = client.get(robots_url, timeout=10.0)
    except httpx.TimeoutException:
        return RobotsResult(False, "timeout", None, fetched_at, None, None)
    except httpx.TransportError:
        return RobotsResult(False, "unreachable", None, fetched_at, None, None)

    if resp.status_code == 404:
        return RobotsResult(False, "missing", 404, fetched_at, None, None)
    if resp.status_code == 429:
        return RobotsResult(False, "rate_limited", 429, fetched_at, None, None)
    if 500 <= resp.status_code < 600:
        return RobotsResult(False, "server_error", resp.status_code, fetched_at, None, None)
    if resp.status_code != 200:
        return RobotsResult(False, f"unexpected_status_{resp.status_code}", resp.status_code, fetched_at, None, None)

    body = resp.content
    if b"\x00" in body:
        return RobotsResult(False, "malformed_bytes", 200, fetched_at, hashlib.sha256(body).hexdigest(), len(body))
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return RobotsResult(False, "malformed_encoding", 200, fetched_at, hashlib.sha256(body).hexdigest(), len(body))

    parser = Protego.parse(text)
    return RobotsResult(
        ok=True,
        reason=None,
        status_code=200,
        fetched_at=fetched_at,
        sha256=hashlib.sha256(body).hexdigest(),
        byte_count=len(body),
        parser=parser,
    )


def can_fetch(result: RobotsResult, url: str, user_agent: str) -> bool:
    if not result.ok or result.parser is None:
        return False
    return bool(result.parser.can_fetch(url, user_agent))
