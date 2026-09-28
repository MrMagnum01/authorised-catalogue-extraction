"""HTML parsing for the two fixture template generations (v1, v2).

The fixture site's markup contract:

v1 listing:       <ul class="product-list">
                     <li class="product-item"><a class="product-link" href="...">Name</a></li>
                     <a class="next-page" href="...">Next</a>
                   </ul>
v1 detail:        <div class="product-detail" data-product-id="ID">
                     <h1 class="prod-name">Name</h1>
                     <span class="prod-price" data-currency="CCY">1999</span>
                     <span class="prod-category">Cat</span>
                   </div>

v2 listing:       <ul class="item-list">
                     <li class="item-row"><a class="item-link" href="...">Name</a></li>
                     <a class="pager-next" href="...">Next</a>
                   </ul>
v2 detail:        <article class="item" id="item-ID">
                     <h2 class="item-title">Name</h2>
                     <table class="item-info">
                       <tr><td>Price</td><td><span class="price-value" data-ccy="CCY">1999</span></td></tr>
                       <tr><td>Category</td><td class="item-category">Cat</td></tr>
                     </table>
                   </article>

Anything matching neither signature is `template_unrecognised` — we
never guess at fields from an unknown layout. A page carrying both a v1
*and* a v2 detail container, or more than one container of the same
version, or more than one match for a single required field within one
container, is `ambiguous` — every field lookup is scoped to its single
detail container, never to the whole page, so a second product's markup
elsewhere on the page (or a second conflicting element inside the same
container) can never silently supply this record's data.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Optional

from selectolax.parser import HTMLParser

from . import PARSER_VERSION
from .models import Money, RawProductRecord


@dataclass
class ListingParseResult:
    template: str  # "v1" | "v2" | "unrecognised"
    product_links: list[str]
    next_link: Optional[str]


def parse_listing(html: bytes, base_url: str) -> ListingParseResult:
    """Template is detected from the list *container*, not its items, so a
    valid empty page (zero products) still identifies as v1/v2 rather than
    falling through to unrecognised."""
    tree = HTMLParser(html)
    related = [a.attributes.get("href") for a in tree.css("a.related") if a.attributes.get("href")]

    if tree.css_first("ul.product-list") is not None:
        links = [a.attributes.get("href") for a in tree.css("a.product-link") if a.attributes.get("href")]
        next_node = tree.css_first("a.next-page")
        next_link = next_node.attributes.get("href") if next_node else None
        return ListingParseResult("v1", links + related, next_link)
    if tree.css_first("ul.item-list") is not None:
        links = [a.attributes.get("href") for a in tree.css("a.item-link") if a.attributes.get("href")]
        next_node = tree.css_first("a.pager-next")
        next_link = next_node.attributes.get("href") if next_node else None
        return ListingParseResult("v2", links + related, next_link)
    return ListingParseResult("unrecognised", related, None)


def _content_hash(product_id: Optional[str], name: Optional[str], price: Optional[Money], category: Optional[str]) -> str:
    payload = json.dumps(
        {
            "id": product_id,
            "name": name,
            "amount_minor": price.amount_minor if price else None,
            "currency": price.currency if price else None,
            "category": category,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scoped_field(container, selector: str) -> tuple[Optional[object], str]:
    """Look up `selector` within `container` only. Returns (node_or_None, status)
    where status is "ok" (exactly one match), "missing" (zero) or "ambiguous" (more than one)."""
    nodes = container.css(selector)
    if not nodes:
        return None, "missing"
    if len(nodes) > 1:
        return None, "ambiguous"
    return nodes[0], "ok"


def _parse_price(text: Optional[str], currency: Optional[str]) -> tuple[Optional[Money], str]:
    if text is None or currency is None:
        return None, "unknown"
    text = text.strip()
    if not text:
        return None, "unknown"
    try:
        amount = int(text)
    except ValueError:
        return None, "ambiguous"
    try:
        return Money(amount_minor=amount, currency=currency.strip().upper()), "ok"
    except ValueError:
        return None, "ambiguous"


def _scoped_price(container, selector: str, currency_attr: str) -> tuple[Optional[Money], str]:
    node, status = _scoped_field(container, selector)
    if status == "ambiguous":
        return None, "ambiguous"
    if status == "missing":
        return None, "unknown"
    return _parse_price(node.text(strip=True), node.attributes.get(currency_attr))


def parse_detail(html: bytes, url: str) -> RawProductRecord:
    tree = HTMLParser(html)
    content_hash_of_bytes = hashlib.sha256(html).hexdigest()

    v1_nodes = tree.css("div.product-detail[data-product-id]")
    v2_nodes = [n for n in tree.css("article.item[id]") if (n.attributes.get("id") or "").startswith("item-")]

    if v1_nodes and v2_nodes:
        return _rejected("ambiguous_field:mixed_template", None, "ambiguous", url, content_hash_of_bytes)

    if v1_nodes:
        if len(v1_nodes) > 1:
            return _rejected("ambiguous_field:container", None, "ambiguous", url, content_hash_of_bytes)
        container = v1_nodes[0]
        product_id = container.attributes.get("data-product-id")
        name, name_status = _scoped_text(container, "h1.prod-name")
        category, category_status = _scoped_text(container, "span.prod-category")
        price, price_status = _scoped_price(container, "span.prod-price", "data-currency")
        return _finish_record(
            product_id, name, name_status, price, price_status, category, category_status,
            "v1", url, content_hash_of_bytes,
        )

    if v2_nodes:
        if len(v2_nodes) > 1:
            return _rejected("ambiguous_field:container", None, "ambiguous", url, content_hash_of_bytes)
        container = v2_nodes[0]
        product_id = (container.attributes.get("id") or "")[len("item-"):]
        name, name_status = _scoped_text(container, "h2.item-title")
        category, category_status = _scoped_text(container, "td.item-category")
        price, price_status = _scoped_price(container, "span.price-value", "data-ccy")
        return _finish_record(
            product_id, name, name_status, price, price_status, category, category_status,
            "v2", url, content_hash_of_bytes,
        )

    return RawProductRecord(
        product_id=None,
        name=None,
        price=None,
        price_status="unknown",
        category=None,
        template="unrecognised",
        source_url=url,
        content_hash=content_hash_of_bytes,
        outcome="template_unrecognised",
        reason="no known template signature matched",
    )


def _scoped_text(container, selector: str) -> tuple[Optional[str], str]:
    node, status = _scoped_field(container, selector)
    if status != "ok":
        return None, status
    return node.text(strip=True), "ok"


def _rejected(reason: str, product_id, template, url, raw_hash) -> RawProductRecord:
    return RawProductRecord(
        product_id=product_id, name=None, price=None, price_status="unknown",
        category=None, template=template, source_url=url, content_hash=raw_hash,
        outcome="rejected", reason=reason,
    )


def _finish_record(
    product_id, name, name_status, price, price_status, category, category_status,
    template, url, raw_hash,
) -> RawProductRecord:
    ambiguous = [
        field_name for field_name, status in (
            ("name", name_status), ("price", price_status), ("category", category_status),
        ) if status == "ambiguous"
    ]
    if ambiguous:
        return RawProductRecord(
            product_id=product_id, name=name, price=price, price_status=price_status,
            category=category, template=template, source_url=url, content_hash=raw_hash,
            outcome="rejected", reason=f"ambiguous_field:{'+'.join(ambiguous)}",
        )
    if not product_id or not name:
        return RawProductRecord(
            product_id=product_id, name=name, price=price, price_status=price_status,
            category=category, template=template, source_url=url, content_hash=raw_hash,
            outcome="rejected", reason="missing_required_field",
        )
    if price_status != "ok":
        return RawProductRecord(
            product_id=product_id, name=name, price=price, price_status=price_status,
            category=category, template=template, source_url=url, content_hash=raw_hash,
            outcome="rejected", reason=f"price_{price_status}",
        )
    content_hash = _content_hash(product_id, name, price, category)
    return RawProductRecord(
        product_id=product_id, name=name, price=price, price_status=price_status,
        category=category, template=template, source_url=url, content_hash=content_hash,
        outcome="accepted", reason=None,
    )
