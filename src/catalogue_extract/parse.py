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
never guess at fields from an unknown layout.
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


def parse_detail(html: bytes, url: str) -> RawProductRecord:
    tree = HTMLParser(html)
    content_hash_of_bytes = hashlib.sha256(html).hexdigest()

    v1_node = tree.css_first("div.product-detail[data-product-id]")
    if v1_node is not None:
        product_id = v1_node.attributes.get("data-product-id")
        name_node = tree.css_first("h1.prod-name")
        price_node = tree.css_first("span.prod-price")
        cat_node = tree.css_first("span.prod-category")
        name = name_node.text(strip=True) if name_node else None
        category = cat_node.text(strip=True) if cat_node else None
        price, price_status = _parse_price(
            price_node.text(strip=True) if price_node else None,
            price_node.attributes.get("data-currency") if price_node else None,
        )
        return _finish_record(product_id, name, price, price_status, category, "v1", url, content_hash_of_bytes)

    v2_node = tree.css_first("article.item[id]")
    if v2_node is not None and (v2_node.attributes.get("id") or "").startswith("item-"):
        product_id = (v2_node.attributes.get("id") or "")[len("item-"):]
        title_node = tree.css_first("h2.item-title")
        price_node = tree.css_first("span.price-value")
        cat_node = tree.css_first("td.item-category")
        name = title_node.text(strip=True) if title_node else None
        category = cat_node.text(strip=True) if cat_node else None
        price, price_status = _parse_price(
            price_node.text(strip=True) if price_node else None,
            price_node.attributes.get("data-ccy") if price_node else None,
        )
        return _finish_record(product_id, name, price, price_status, category, "v2", url, content_hash_of_bytes)

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


def _finish_record(product_id, name, price, price_status, category, template, url, raw_hash) -> RawProductRecord:
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
