"""Renders the v1/v2 listing and detail HTML the parser is built against."""
from __future__ import annotations

from html import escape
from typing import Optional

from .data import FixtureProduct

BAIT_LINKS_TEMPLATE = """
<div class="related-links">
  <a class="related" href="http://192.0.2.1/external">external-absolute</a>
  <a class="related" href="//192.0.2.1/external">external-protocol-relative</a>
  <a class="related" href="{other_origin}/catalogue/">other-loopback-service</a>
  <a class="related" href="http://user:pass@{host_port}/catalogue/">creds-in-url</a>
  <a class="related" href="/catalogue/%2e%2e/secret">encoded-traversal</a>
  <a class="related" href="/_redirect/outside">redirect-outside-origin</a>
  <a class="related" href="/_redirect/loop">redirect-loop</a>
</div>
"""


def render_listing(
    template_version: str,
    products: list[FixtureProduct],
    next_link: Optional[str],
    include_bait_links: bool = False,
    other_origin: str = "http://127.0.0.1:1",
    host_port: str = "127.0.0.1:1",
    extra_links: Optional[list[str]] = None,
) -> str:
    if template_version == "v1":
        items = "\n".join(
            f'<li class="product-item"><a class="product-link" href="/catalogue/product/{escape(p.id)}">{escape(p.name)}</a></li>'
            for p in products
        )
        next_html = f'<a class="next-page" href="{escape(next_link)}">Next</a>' if next_link else ""
        body = f'<ul class="product-list">{items}</ul><nav class="pagination">{next_html}</nav>'
    elif template_version == "v2":
        items = "\n".join(
            f'<li class="item-row"><a class="item-link" href="/catalogue/product/{escape(p.id)}">{escape(p.name)}</a></li>'
            for p in products
        )
        next_html = f'<a class="pager-next" href="{escape(next_link)}">Next</a>' if next_link else ""
        body = f'<ul class="item-list">{items}</ul><nav class="pagination">{next_html}</nav>'
    else:
        # Neither known signature: exercises template_unrecognised at the listing level.
        items = "\n".join(f'<span data-ref="{escape(p.id)}">{escape(p.name)}</span>' for p in products)
        body = f'<div class="mystery-layout">{items}</div>'

    bait = BAIT_LINKS_TEMPLATE.format(other_origin=other_origin, host_port=host_port) if include_bait_links else ""
    extra_html = "".join(f'<a class="related" href="{escape(href)}">extra</a>' for href in (extra_links or []))
    return f"<!DOCTYPE html><html><body>{body}{bait}{extra_html}</body></html>"


def render_detail_v1(p: FixtureProduct) -> str:
    return (
        "<!DOCTYPE html><html><body>"
        f'<div class="product-detail" data-product-id="{escape(p.id)}">'
        f'<h1 class="prod-name">{escape(p.name)}</h1>'
        f'<span class="prod-price" data-currency="{escape(p.currency)}">{p.amount_minor}</span>'
        f'<span class="prod-category">{escape(p.category)}</span>'
        "</div></body></html>"
    )


def render_detail_v2(p: FixtureProduct) -> str:
    return (
        "<!DOCTYPE html><html><body>"
        f'<article class="item" id="item-{escape(p.id)}">'
        f'<h2 class="item-title">{escape(p.name)}</h2>'
        '<table class="item-info"><tr><td>Price</td><td>'
        f'<span class="price-value" data-ccy="{escape(p.currency)}">{p.amount_minor}</span>'
        f'</td></tr><tr><td>Category</td><td class="item-category">{escape(p.category)}</td></tr></table>'
        "</article></body></html>"
    )


def render_detail_unrecognised(p: FixtureProduct) -> str:
    return (
        "<!DOCTYPE html><html><body>"
        f'<section class="thing" data-ref="{escape(p.id)}">'
        f'<span>{escape(p.name)}</span>'
        "</section></body></html>"
    )


def render_detail_missing_price(p: FixtureProduct) -> str:
    return (
        "<!DOCTYPE html><html><body>"
        f'<div class="product-detail" data-product-id="{escape(p.id)}">'
        f'<h1 class="prod-name">{escape(p.name)}</h1>'
        "</div></body></html>"
    )


def render_detail_ambiguous_price(p: FixtureProduct) -> str:
    return (
        "<!DOCTYPE html><html><body>"
        f'<div class="product-detail" data-product-id="{escape(p.id)}">'
        f'<h1 class="prod-name">{escape(p.name)}</h1>'
        f'<span class="prod-price" data-currency="{escape(p.currency)}">call for price</span>'
        "</div></body></html>"
    )
