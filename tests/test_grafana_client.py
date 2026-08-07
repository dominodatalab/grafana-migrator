import pytest

from grafana_migrator.grafana_client import GrafanaClient, normalize_source_url


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


def test_normalize_source_url_leaves_url_untouched_when_no_path_segment_given():
    assert normalize_source_url("https://example-cluster.example.com") == "https://example-cluster.example.com"


def test_normalize_source_url_strips_trailing_slash_even_with_no_path_segment():
    assert normalize_source_url("https://example-cluster.example.com/") == "https://example-cluster.example.com"


def test_normalize_source_url_appends_path_segment_when_given_and_missing():
    assert (
        normalize_source_url("https://example-cluster.example.com", "grafana")
        == "https://example-cluster.example.com/grafana"
    )


def test_normalize_source_url_leaves_path_segment_alone_when_already_present():
    assert (
        normalize_source_url("https://example-cluster.example.com/grafana", "grafana")
        == "https://example-cluster.example.com/grafana"
    )


def test_normalize_source_url_strips_trailing_slash_either_way():
    assert (
        normalize_source_url("https://example-cluster.example.com/", "grafana")
        == "https://example-cluster.example.com/grafana"
    )
    assert (
        normalize_source_url("https://example-cluster.example.com/grafana/", "grafana")
        == "https://example-cluster.example.com/grafana"
    )


def test_normalize_source_url_leaves_localhost_port_forward_url_untouched():
    # kubectl port-forward hits the Service root -- no ingress path prefix to add.
    assert normalize_source_url("http://localhost:18090", "grafana") == "http://localhost:18090"
    assert normalize_source_url("http://127.0.0.1:18090/", "grafana") == "http://127.0.0.1:18090"


def test_grafana_client_does_not_alter_base_url_by_default():
    client = GrafanaClient("https://example-cluster.example.com", token="glsa_faketoken")
    assert client.base_url == "https://example-cluster.example.com"


def test_grafana_client_appends_source_path_segment_when_given():
    client = GrafanaClient(
        "https://example-cluster.example.com", token="glsa_faketoken", source_path_segment="grafana"
    )
    assert client.base_url == "https://example-cluster.example.com/grafana"
