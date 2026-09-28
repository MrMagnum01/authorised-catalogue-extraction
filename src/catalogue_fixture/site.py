"""The synthetic fixture site: a threaded HTTP server plus a mutable,
test-controlled FixtureState. Every test builds its own FixtureState and
spins up a private server on an ephemeral loopback port, so tests never
share fixture state or timing.
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional
from urllib.parse import parse_qs, urlsplit

from . import DEFAULT_PERMISSION_TEMPLATE
from .data import FixtureProduct
from .templates import (
    render_detail_v1,
    render_detail_v2,
    render_listing,
)


@dataclass
class Injection:
    path: str
    status: int
    remaining: int
    retry_after: Optional[str] = None


@dataclass
class FixtureState:
    products: list[FixtureProduct] = field(default_factory=list)
    page_size: int = 10
    template_version: str = "v1"
    robots_mode: str = "normal"
    robots_disallow: tuple = ("/catalogue/product/blocked",)
    permission_mode: str = "granted"  # "granted" | "denied" | "wrong_site" | "narrow_paths" | "malformed"
    permission_text_override: Optional[str] = None
    permission_missing: bool = False
    snapshot_id: str = "snap-0001"
    catalogue_version: int = 1
    bump_version_after: Optional[int] = None
    include_bait_links: bool = False
    other_origin: str = "http://127.0.0.1:1"
    special_products: dict = field(default_factory=dict)  # id -> raw html str
    extra_listing_links: list = field(default_factory=list)  # extra hrefs to surface from the listing page
    redirect_map: dict = field(default_factory=dict)  # request path -> 302 Location
    advertised_items_override: Optional[int] = None  # override len(products) in /_meta/snapshot

    def __post_init__(self) -> None:
        self._lock = threading.Lock()
        self._injections: list[Injection] = []
        self.request_count = 0

    def add_injection(self, path: str, status: int, times: int = 1, retry_after: Optional[str] = None) -> None:
        with self._lock:
            self._injections.append(Injection(path, status, times, retry_after))

    def consume_injection(self, path: str) -> Optional[Injection]:
        with self._lock:
            for inj in self._injections:
                if inj.path == path and inj.remaining > 0:
                    inj.remaining -= 1
                    return inj
        return None

    def note_page_request(self) -> None:
        with self._lock:
            self.request_count += 1
            if self.bump_version_after is not None and self.request_count == self.bump_version_after:
                self.catalogue_version += 1
                self.snapshot_id = f"snap-{self.catalogue_version:04d}"

    def advertised_pages(self) -> int:
        if not self.products:
            return 1
        return math.ceil(len(self.products) / self.page_size)

    def robots_body(self) -> str:
        lines = ["User-agent: *"]
        for p in self.robots_disallow:
            lines.append(f"Disallow: {p}")
        lines.append("Allow: /catalogue/")
        return "\n".join(lines) + "\n"

    def permission_body(self, origin: str) -> str:
        if self.permission_text_override is not None:
            return self.permission_text_override
        if self.permission_mode == "denied":
            return f"Site: {origin}\nAllowed-Paths: /catalogue/\nPurpose: demo\nPermission: denied\n"
        if self.permission_mode == "wrong_site":
            return "Site: http://127.0.0.1:1\nAllowed-Paths: /catalogue/\nPurpose: demo\nPermission: granted\n"
        if self.permission_mode == "narrow_paths":
            return f"Site: {origin}\nAllowed-Paths: /catalogue/only-a-subpath/\nPurpose: demo\nPermission: granted\n"
        if self.permission_mode == "malformed":
            return "Explicitly forbidden to crawl"
        return DEFAULT_PERMISSION_TEMPLATE.format(origin=origin)


def _make_handler(state: FixtureState):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):  # silence per-request stderr noise
            pass

        def _send_bytes(self, status: int, body: bytes, content_type: str = "text/plain", extra_headers: Optional[dict] = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _send_text(self, status: int, text: str, content_type: str = "text/plain; charset=utf-8") -> None:
            self._send_bytes(status, text.encode("utf-8"), content_type)

        def _send_json(self, status: int, obj: dict) -> None:
            self._send_text(status, json.dumps(obj), "application/json")

        def _send_redirect(self, status: int, location: str) -> None:
            self._send_bytes(status, b"", extra_headers={"Location": location})

        def do_GET(self) -> None:  # noqa: N802 (stdlib naming)
            parsed = urlsplit(self.path)
            path = parsed.path
            query = parse_qs(parsed.query)

            inj = state.consume_injection(path)
            if inj is not None:
                headers = {"Retry-After": inj.retry_after} if inj.retry_after else {}
                self._send_bytes(inj.status, b"", extra_headers=headers)
                return

            if path in state.redirect_map:
                return self._send_redirect(302, state.redirect_map[path])

            if path == "/robots.txt":
                return self._handle_robots()
            if path == "/PERMISSION.md":
                if state.permission_missing:
                    return self._send_text(404, "not found")
                origin = f"http://{self.headers.get('Host', '127.0.0.1')}"
                return self._send_text(200, state.permission_body(origin))
            if path == "/_meta/snapshot":
                items = state.advertised_items_override
                if items is None:
                    items = len(state.products)
                return self._send_json(200, {
                    "snapshot_id": state.snapshot_id,
                    "catalogue_version": state.catalogue_version,
                    "advertised_pages": state.advertised_pages(),
                    "advertised_items": items,
                })
            if path in ("/catalogue", "/catalogue/"):
                state.note_page_request()
                page = int((query.get("page") or ["1"])[0])
                return self._handle_listing(page)
            if path.startswith("/catalogue/product/"):
                state.note_page_request()
                pid = path[len("/catalogue/product/"):]
                return self._handle_detail(pid)
            if path == "/_redirect/outside":
                return self._send_redirect(302, "http://192.0.2.1/external")
            if path == "/_redirect/loop":
                return self._send_redirect(302, "/_redirect/loop")
            return self._send_text(404, "not found")

        def _handle_robots(self) -> None:
            mode = state.robots_mode
            if mode == "missing":
                return self._send_text(404, "not found")
            if mode == "error500":
                return self._send_text(500, "internal error")
            if mode == "ratelimited":
                return self._send_bytes(429, b"", extra_headers={"Retry-After": "1"})
            if mode == "malformed":
                return self._send_bytes(200, b"\xff\xfe\x00bad-bytes", content_type="text/plain")
            return self._send_text(200, state.robots_body())

        def _handle_listing(self, page: int) -> None:
            total_pages = state.advertised_pages()
            start = (page - 1) * state.page_size
            chunk = state.products[start:start + state.page_size]
            next_link = f"/catalogue/?page={page + 1}" if page < total_pages else None
            html = render_listing(
                state.template_version, chunk, next_link,
                include_bait_links=state.include_bait_links,
                other_origin=state.other_origin,
                host_port=self.headers.get("Host", "127.0.0.1:1"),
                extra_links=state.extra_listing_links if page == 1 else None,
            )
            self._send_text(200, html, "text/html; charset=utf-8")

        def _handle_detail(self, pid: str) -> None:
            if pid in state.special_products:
                return self._send_text(200, state.special_products[pid], "text/html; charset=utf-8")
            for p in state.products:
                if p.id == pid:
                    html = render_detail_v1(p) if state.template_version == "v1" else render_detail_v2(p)
                    return self._send_text(200, html, "text/html; charset=utf-8")
            return self._send_text(404, "not found")

    return Handler


class FixtureServer:
    def __init__(self, state: Optional[FixtureState] = None):
        self.state = state or FixtureState()
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _make_handler(self.state))
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    @property
    def port(self) -> int:
        return self._httpd.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def start(self) -> "FixtureServer":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=5)

    def __enter__(self) -> "FixtureServer":
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
