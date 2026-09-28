import pytest

from catalogue_extract.boundary import BoundaryError, Scope, canonical_url, check_in_scope, is_in_scope


@pytest.fixture
def scope():
    return Scope.from_origin("http://127.0.0.1:8000", ["/catalogue"])


def test_in_scope_ok(scope):
    assert check_in_scope("http://127.0.0.1:8000/catalogue/product/SKU-0001", scope) == "/catalogue/product/SKU-0001"


def test_root_path_out_of_scope(scope):
    with pytest.raises(BoundaryError) as exc:
        check_in_scope("http://127.0.0.1:8000/", scope)
    assert exc.value.reason == "path_out_of_scope"


def test_credentials_in_url_rejected(scope):
    with pytest.raises(BoundaryError) as exc:
        check_in_scope("http://user:pass@127.0.0.1:8000/catalogue/", scope)
    assert exc.value.reason == "credentials_in_url"


def test_scheme_mismatch_rejected(scope):
    with pytest.raises(BoundaryError) as exc:
        check_in_scope("https://127.0.0.1:8000/catalogue/", scope)
    assert exc.value.reason == "scheme_mismatch"


def test_alternate_port_rejected(scope):
    with pytest.raises(BoundaryError) as exc:
        check_in_scope("http://127.0.0.1:9000/catalogue/", scope)
    assert exc.value.reason == "port_mismatch"


def test_alternate_host_rejected(scope):
    with pytest.raises(BoundaryError) as exc:
        check_in_scope("http://192.0.2.1:8000/catalogue/", scope)
    assert exc.value.reason == "host_mismatch"


def test_another_loopback_service_is_not_authorised(scope):
    # A second service on 127.0.0.1 but a different port is out of scope,
    # exactly like a public third-party host — "localhost" is not special.
    with pytest.raises(BoundaryError) as exc:
        check_in_scope("http://127.0.0.1:8001/catalogue/", scope)
    assert exc.value.reason == "port_mismatch"


def test_protocol_relative_bait_resolves_to_external_host(scope):
    import httpx
    resolved = str(httpx.URL("http://127.0.0.1:8000/catalogue/").join("//192.0.2.1/other"))
    with pytest.raises(BoundaryError) as exc:
        check_in_scope(resolved, scope)
    assert exc.value.reason == "host_mismatch"


def test_encoded_path_traversal_rejected(scope):
    with pytest.raises(BoundaryError) as exc:
        check_in_scope("http://127.0.0.1:8000/catalogue/%2e%2e/secret", scope)
    assert exc.value.reason == "path_out_of_scope"


def test_double_encoded_traversal_out_of_prefix(scope):
    with pytest.raises(BoundaryError):
        check_in_scope("http://127.0.0.1:8000/catalogue/..%2f..%2fsecret", scope)


def test_redirect_outside_origin_rejected(scope):
    with pytest.raises(BoundaryError) as exc:
        check_in_scope("http://192.0.2.1/external", scope)
    assert exc.value.reason == "host_mismatch"


def test_is_in_scope_false_on_violation(scope):
    assert is_in_scope("http://192.0.2.1/x", scope) is False
    assert is_in_scope("http://127.0.0.1:8000/catalogue/x", scope) is True


def test_canonical_url_dedupes_query_order():
    a = canonical_url("http://127.0.0.1:8000/catalogue/?page=2&x=1")
    b = canonical_url("http://127.0.0.1:8000/catalogue/?x=1&page=2")
    assert a == b


def test_canonical_url_dedupes_default_port():
    a = canonical_url("http://127.0.0.1/catalogue/")
    b = canonical_url("http://127.0.0.1:80/catalogue/")
    assert a == b


def test_no_allowed_prefixes_rejected():
    with pytest.raises(BoundaryError):
        Scope.from_origin("http://127.0.0.1:8000", [])
