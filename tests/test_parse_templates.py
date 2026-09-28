from catalogue_extract.parse import parse_detail, parse_listing
from catalogue_fixture.data import FixtureProduct
from catalogue_fixture.templates import (
    render_detail_ambiguous_price,
    render_detail_missing_price,
    render_detail_unrecognised,
    render_detail_v1,
    render_detail_v2,
    render_listing,
)

P = FixtureProduct(id="SKU-0001", name="Widget A", amount_minor=1999, currency="USD", category="Widgets")


def test_v1_detail_parses():
    record = parse_detail(render_detail_v1(P).encode(), "http://x/catalogue/product/SKU-0001")
    assert record.outcome == "accepted"
    assert record.product_id == "SKU-0001"
    assert record.price.amount_minor == 1999
    assert record.price.currency == "USD"
    assert record.template == "v1"


def test_v2_detail_parses_same_data_different_markup():
    record = parse_detail(render_detail_v2(P).encode(), "http://x/catalogue/product/SKU-0001")
    assert record.outcome == "accepted"
    assert record.product_id == "SKU-0001"
    assert record.price.amount_minor == 1999
    assert record.template == "v2"


def test_unrecognised_template_flagged_not_guessed():
    record = parse_detail(render_detail_unrecognised(P).encode(), "http://x/catalogue/product/SKU-0001")
    assert record.outcome == "template_unrecognised"
    assert record.product_id is None
    assert record.price is None


def test_missing_price_rejected_not_zero():
    record = parse_detail(render_detail_missing_price(P).encode(), "http://x/catalogue/product/SKU-0001")
    assert record.outcome == "rejected"
    assert record.price is None
    assert record.price_status == "unknown"


def test_ambiguous_price_rejected_not_zero():
    record = parse_detail(render_detail_ambiguous_price(P).encode(), "http://x/catalogue/product/SKU-0001")
    assert record.outcome == "rejected"
    assert record.price is None
    assert record.price_status == "ambiguous"


def test_v1_listing_links_and_next():
    html = render_listing("v1", [P], next_link="/catalogue/?page=2").encode()
    result = parse_listing(html, "http://x/catalogue/")
    assert result.template == "v1"
    assert result.product_links == ["/catalogue/product/SKU-0001"]
    assert result.next_link == "/catalogue/?page=2"


def test_v2_listing_links_and_next():
    html = render_listing("v2", [P], next_link="/catalogue/?page=2").encode()
    result = parse_listing(html, "http://x/catalogue/")
    assert result.template == "v2"
    assert result.product_links == ["/catalogue/product/SKU-0001"]


def test_empty_listing_still_identifies_template_v1():
    html = render_listing("v1", [], next_link=None).encode()
    result = parse_listing(html, "http://x/catalogue/")
    assert result.template == "v1"
    assert result.product_links == []
    assert result.next_link is None


def test_unrecognised_listing_flagged():
    html = b"<html><body><p>totally different site</p></body></html>"
    result = parse_listing(html, "http://x/catalogue/")
    assert result.template == "unrecognised"


def test_two_conflicting_prices_in_one_container_rejected_as_ambiguous():
    """Astra HOLD group 6: a second, conflicting price element inside the
    same detail container must not be silently ignored in favour of the
    first (`css_first`) match."""
    html = (
        b'<div class="product-detail" data-product-id="P1">'
        b'<h1 class="prod-name">One</h1>'
        b'<span class="prod-price" data-currency="USD">100</span>'
        b'<span class="prod-price" data-currency="EUR">999</span>'
        b'<span class="prod-category">A</span>'
        b"</div>"
    )
    record = parse_detail(html, "http://x/catalogue/product/P1")
    assert record.outcome == "rejected"
    assert record.price is None
    assert "price" in record.reason


def test_two_detail_containers_on_one_page_rejected_as_ambiguous():
    html = (
        render_detail_v1(P).encode()[:-len("</body></html>")]
        + render_detail_v1(FixtureProduct(id="SKU-0002", name="Widget B", amount_minor=500, currency="USD", category="Widgets")).encode()
        + b"</body></html>"
    )
    record = parse_detail(html, "http://x/catalogue/product/SKU-0001")
    assert record.outcome == "rejected"
    assert "ambiguous_field:container" in record.reason


def test_mixed_v1_and_v2_containers_on_one_page_rejected():
    mixed = (
        render_detail_v1(P).encode()[:-len("</body></html>")]
        + render_detail_v2(P).encode()[len("<!DOCTYPE html><html><body>"):]
    )
    record = parse_detail(mixed, "http://x/catalogue/product/SKU-0001")
    assert record.outcome == "rejected"
    assert "mixed_template" in record.reason


def test_fields_scoped_to_container_not_whole_page():
    """A field selector must never reach outside its own detail container
    for a value, even when a second, unrelated element with the same
    class exists elsewhere on the page (e.g. a related-product teaser)."""
    other_price = b'<span class="prod-price" data-currency="EUR">1</span>'
    html = other_price + render_detail_v1(P).encode()
    record = parse_detail(html, "http://x/catalogue/product/SKU-0001")
    assert record.outcome == "accepted"
    assert record.price.amount_minor == 1999
    assert record.price.currency == "USD"
