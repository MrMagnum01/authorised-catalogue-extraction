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
