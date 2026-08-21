import pytest
import requests
from fake_session import FakeResponse, FakeSession

from grafana_migrator.grafana_client import (
    GrafanaAuthError,
    GrafanaBadRequestError,
    GrafanaClient,
    GrafanaClientError,
    GrafanaConflictError,
    GrafanaForbiddenError,
    GrafanaNotFoundError,
    GrafanaServerError,
    build_client,
    normalize_base_url,
)


def test_token_auth_sets_bearer_header_not_basic_auth():
    client = GrafanaClient("http://localhost:18090", token="glsa_faketoken")
    assert client.session.headers["Authorization"] == "Bearer glsa_faketoken"
    assert client.session.auth is None


def test_basic_auth_sets_session_auth_not_bearer_header():
    client = GrafanaClient("http://localhost:18090", auth=("grafana", "hunter2"))
    assert client.session.auth == ("grafana", "hunter2")
    assert "Authorization" not in client.session.headers


def test_requires_either_auth_or_token():
    with pytest.raises(ValueError):
        GrafanaClient("http://localhost:18090")


def test_token_takes_precedence_if_both_given():
    client = GrafanaClient("http://localhost:18090", auth=("grafana", "hunter2"), token="glsa_faketoken")
    assert client.session.headers["Authorization"] == "Bearer glsa_faketoken"


def test_normalize_base_url_leaves_url_untouched_when_no_path_segment_given():
    assert normalize_base_url("https://example-cluster.example.com") == "https://example-cluster.example.com"


def test_normalize_base_url_strips_trailing_slash_even_with_no_path_segment():
    assert normalize_base_url("https://example-cluster.example.com/") == "https://example-cluster.example.com"


def test_normalize_base_url_appends_path_segment_when_given_and_missing():
    assert (
        normalize_base_url("https://example-cluster.example.com", "grafana")
        == "https://example-cluster.example.com/grafana"
    )


def test_normalize_base_url_leaves_path_segment_alone_when_already_present():
    assert (
        normalize_base_url("https://example-cluster.example.com/grafana", "grafana")
        == "https://example-cluster.example.com/grafana"
    )


def test_normalize_base_url_strips_trailing_slash_either_way():
    assert (
        normalize_base_url("https://example-cluster.example.com/", "grafana")
        == "https://example-cluster.example.com/grafana"
    )
    assert (
        normalize_base_url("https://example-cluster.example.com/grafana/", "grafana")
        == "https://example-cluster.example.com/grafana"
    )


def test_normalize_base_url_leaves_localhost_port_forward_url_untouched():
    # kubectl port-forward hits the Service root -- no ingress path prefix to add.
    assert normalize_base_url("http://localhost:18090", "grafana") == "http://localhost:18090"
    assert normalize_base_url("http://127.0.0.1:18090/", "grafana") == "http://127.0.0.1:18090"


def test_grafana_client_does_not_alter_base_url_by_default():
    client = GrafanaClient("https://example-cluster.example.com", token="glsa_faketoken")
    assert client.base_url == "https://example-cluster.example.com"


def test_grafana_client_appends_path_segment_when_given():
    client = GrafanaClient("https://example-cluster.example.com", token="glsa_faketoken", path_segment="grafana")
    assert client.base_url == "https://example-cluster.example.com/grafana"


# ---------------------------------------------------------------------------
# request layer + error classification
# ---------------------------------------------------------------------------


def _client(routes, **kw):
    return GrafanaClient("http://graf.test", token="glsa_faketoken", session=FakeSession(routes), **kw)


def test_get_still_routes_through_request_and_returns_parsed_json():
    client = _client({("GET", "/api/search"): FakeResponse(200, [{"uid": "abc"}])})
    assert client.search() == [{"uid": "abc"}]
    assert client.session.calls[0][0] == "GET"
    assert client.session.calls[0][1] == "/api/search"


@pytest.mark.parametrize(
    "status,expected",
    [
        (401, GrafanaAuthError),
        (403, GrafanaForbiddenError),
        (404, GrafanaNotFoundError),
        (400, GrafanaBadRequestError),
        (422, GrafanaBadRequestError),
        (409, GrafanaConflictError),
        (412, GrafanaConflictError),
        (500, GrafanaServerError),
        (503, GrafanaServerError),
    ],
)
def test_status_maps_to_error_subclass(status, expected):
    client = _client({("GET", "/api/search"): FakeResponse(status, {"message": "nope"})})
    with pytest.raises(expected):
        client.search()


@pytest.mark.parametrize("status", [401, 403, 404, 400, 409, 500])
def test_every_error_subclass_is_still_a_grafana_client_error(status):
    # source_dump.fetch_source downgrades GrafanaClientError to a warning; the
    # subclasses must keep being caught by that existing handler.
    client = _client({("GET", "/api/search"): FakeResponse(status, {"message": "nope"})})
    with pytest.raises(GrafanaClientError):
        client.search()


def test_error_carries_status_and_body_for_reporting():
    client = _client({("GET", "/api/search"): FakeResponse(400, {"message": "bad uid"})})
    with pytest.raises(GrafanaBadRequestError) as excinfo:
        client.search()
    assert excinfo.value.status == 400
    assert "bad uid" in excinfo.value.body
    assert excinfo.value.method == "GET"


def test_transport_error_is_wrapped_with_no_status():
    client = _client({("GET", "/api/search"): requests.ConnectionError("connection refused")})
    with pytest.raises(GrafanaClientError) as excinfo:
        client.search()
    assert excinfo.value.status is None
    assert "connection refused" in str(excinfo.value)


def test_auth_error_message_names_the_flag_prefix_it_was_built_with():
    client = _client({("GET", "/api/search"): FakeResponse(401)}, flag_prefix="dest")
    with pytest.raises(GrafanaAuthError) as excinfo:
        client.search()
    assert "--dest-token" in str(excinfo.value)
    assert "--source-token" not in str(excinfo.value)


def test_forbidden_error_message_names_the_admin_org_role_requirement():
    client = _client({("GET", "/api/search"): FakeResponse(403)})
    with pytest.raises(GrafanaForbiddenError) as excinfo:
        client.search()
    assert "Admin" in str(excinfo.value)


def test_empty_success_body_is_none_not_a_decode_error():
    # Some provisioning writes answer 200/202 with no body.
    client = _client({("GET", "/api/search"): FakeResponse(202, text="")})
    assert client.search() is None


def test_non_json_success_body_raises_grafana_client_error_not_a_decode_error():
    client = _client({("GET", "/api/search"): FakeResponse(200, text="<html>nope</html>")})
    with pytest.raises(GrafanaClientError) as excinfo:
        client.search()
    assert "non-JSON" in str(excinfo.value)


def test_post_is_excluded_from_the_retry_allowlist():
    # urllib3 cannot tell a POST the server never saw from one it processed
    # before the 503, so retrying a create would duplicate objects.
    client = GrafanaClient("http://graf.test", token="t")
    allowed = client.session.get_adapter("http://graf.test").max_retries.allowed_methods
    assert "GET" in allowed
    assert "PUT" in allowed
    assert "POST" not in allowed


def test_default_headers_are_sent_on_every_request():
    client = GrafanaClient(
        "http://graf.test",
        token="t",
        session=FakeSession({("GET", "/api/search"): FakeResponse(200, [])}),
        default_headers={"X-Disable-Provenance": "true"},
    )
    assert client.session.headers["X-Disable-Provenance"] == "true"


# ---------------------------------------------------------------------------
# build_client
# ---------------------------------------------------------------------------


def test_build_client_prefers_token_over_basic_auth():
    client = build_client(url="http://graf.test", token="glsa_tok", user="admin", password="pw")
    assert client.session.headers["Authorization"] == "Bearer glsa_tok"
    assert client.session.auth is None


def test_build_client_falls_back_to_basic_auth():
    client = build_client(url="http://graf.test", user="admin", password="pw")
    assert client.session.auth == ("admin", "pw")
    assert "Authorization" not in client.session.headers


def test_build_client_rejects_missing_credentials_naming_the_flag_prefix():
    with pytest.raises(ValueError) as excinfo:
        build_client(url="http://graf.test", flag_prefix="dest")
    assert "--dest-token" in str(excinfo.value)


def test_build_client_rejects_half_given_basic_auth():
    with pytest.raises(ValueError):
        build_client(url="http://graf.test", user="admin")


def test_build_client_passes_path_segment_through():
    client = build_client(url="https://host.example.com", token="t", path_segment="grafana")
    assert client.base_url == "https://host.example.com/grafana"
