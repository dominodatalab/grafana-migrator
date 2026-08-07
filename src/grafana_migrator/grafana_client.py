"""Minimal HTTP client for the source Grafana instance's HTTP API."""

from __future__ import annotations

import logging
from typing import Any, Optional
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# kubectl port-forward hosts: no ingress path-prefix to add here.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


class GrafanaClientError(RuntimeError):
    """Raised for auth failures, network errors, or unexpected API responses."""


def normalize_source_url(url: str, path_segment: Optional[str] = None) -> str:
    """Strip a trailing slash, and optionally make sure `url` ends in `path_segment`.

    `path_segment` covers ingress path-prefix deployments (e.g. `/grafana`);
    appended only if missing. localhost/127.0.0.1 URLs are always left as-is.
    """
    url = url.rstrip("/")
    if not path_segment:
        return url

    parsed = urlsplit(url)
    if parsed.hostname in _LOCAL_HOSTS:
        return url

    segments = [s for s in parsed.path.split("/") if s]
    if segments and segments[-1] == path_segment:
        return url

    new_path = f"{parsed.path.rstrip('/')}/{path_segment}"
    normalized = urlunsplit((parsed.scheme, parsed.netloc, new_path, parsed.query, parsed.fragment))
    if normalized != url:
        logger.info("--source-url %r didn't end in /%s, using %r instead", url, path_segment, normalized)
    return normalized


class GrafanaClient:
    def __init__(
        self,
        base_url: str,
        auth: Optional[tuple[str, str]] = None,
        token: Optional[str] = None,
        source_path_segment: Optional[str] = None,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
    ) -> None:
        """Talk to a Grafana HTTP API using either basic auth or a service account token.

        Prefer `token`: GF_SECURITY_ADMIN_PASSWORD only applies on first
        admin-user creation, so a stale/rotated secret can silently break
        basic auth on restart.
        """
        if not auth and not token:
            raise ValueError("GrafanaClient requires either auth=(user, password) or token=...")
        self.base_url = normalize_source_url(base_url, source_path_segment)
        self.timeout = timeout
        self.session = session or requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            self.session.auth = auth
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=("GET",),
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _get(self, path: str, **params: Any) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise GrafanaClientError(f"GET {url} failed: {exc}") from exc

        if resp.status_code == 401:
            raise GrafanaClientError(
                f"GET {url} returned 401 Unauthorized -- check --source-token, or "
                "--source-user/--source-password (note: the admin username may not be "
                "'admin' -- check GF_SECURITY_ADMIN_USER on the pod; also note that Grafana "
                "does not reapply GF_SECURITY_ADMIN_PASSWORD to an already-existing admin "
                "user on restart, so a correct secret value can still 401 -- a service "
                "account token avoids this class of problem entirely)"
            )
        if not resp.ok:
            raise GrafanaClientError(f"GET {url} returned {resp.status_code}: {resp.text[:500]}")
        return resp.json()

    def search(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Return every folder + dashboard row from /api/search."""
        return self._get("/api/search", limit=limit)

    def get_dashboard(self, uid: str) -> dict[str, Any]:
        """Return the full {"dashboard": ..., "meta": ...} payload for a uid."""
        return self._get(f"/api/dashboards/uid/{uid}")

    def list_alert_rules(self) -> list[dict[str, Any]]:
        """Return every provisioned alert rule (Grafana-managed unified alerting only)."""
        return self._get("/api/v1/provisioning/alert-rules")

    def list_contact_points(self) -> list[dict[str, Any]]:
        """Return every provisioned contact point.

        Secure settings are redacted by Grafana itself; the response only
        marks which field names are set, via each receiver's `secureFields`.
        """
        return self._get("/api/v1/provisioning/contact-points")

    def get_notification_policy_tree(self) -> dict[str, Any]:
        """Return the single root alertmanager route tree for this org."""
        return self._get("/api/v1/provisioning/policies")
