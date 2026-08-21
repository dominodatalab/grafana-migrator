"""HTTP client for a Grafana instance's HTTP API.

`export` uses the read methods against the source instance. The write methods
(added alongside `import --target api`) talk to a destination instance, so
nothing here is source-specific -- `flag_prefix` is what makes an error message
name `--source-token` or `--dest-token`.
"""

from __future__ import annotations

import logging
from typing import Any, Optional, cast
from urllib.parse import urlsplit, urlunsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

# kubectl port-forward hosts: no ingress path-prefix to add here.
_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "::1"})


class GrafanaClientError(RuntimeError):
    """Raised for auth failures, network errors, or unexpected API responses.

    `status`/`body` are populated whenever the failure came back as an HTTP
    response rather than a transport error, so callers can record them in a
    report instead of re-parsing the message string.
    """

    def __init__(
        self,
        message: str,
        *,
        method: Optional[str] = None,
        url: Optional[str] = None,
        status: Optional[int] = None,
        body: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.url = url
        self.status = status
        self.body = body


class GrafanaAuthError(GrafanaClientError):
    """401: the credentials were rejected. Abort -- every later call fails identically."""


class GrafanaForbiddenError(GrafanaClientError):
    """403: authenticated but not permitted. Abort -- usually an insufficient org role."""


class GrafanaNotFoundError(GrafanaClientError):
    """404: the endpoint or object does not exist."""


class GrafanaBadRequestError(GrafanaClientError):
    """400/422: this payload was rejected. Object-specific -- the caller can keep going."""


class GrafanaConflictError(GrafanaClientError):
    """409/412: the object already exists on the target. Not a failure for an import."""


class GrafanaServerError(GrafanaClientError):
    """5xx surviving the retry policy: the instance is unhealthy. Abort."""


def _error_class_for_status(status: int) -> type[GrafanaClientError]:
    if status == 401:
        return GrafanaAuthError
    if status == 403:
        return GrafanaForbiddenError
    if status == 404:
        return GrafanaNotFoundError
    if status in (400, 422):
        return GrafanaBadRequestError
    if status in (409, 412):
        return GrafanaConflictError
    if status >= 500:
        return GrafanaServerError
    return GrafanaClientError


def normalize_base_url(url: str, path_segment: Optional[str] = None) -> str:
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
        logger.info("url %r didn't end in /%s, using %r instead", url, path_segment, normalized)
    return normalized


class GrafanaClient:
    def __init__(
        self,
        base_url: str,
        auth: Optional[tuple[str, str]] = None,
        token: Optional[str] = None,
        path_segment: Optional[str] = None,
        timeout: float = 15.0,
        session: Optional[requests.Session] = None,
        flag_prefix: str = "source",
        default_headers: Optional[dict[str, str]] = None,
    ) -> None:
        """Talk to a Grafana HTTP API using either basic auth or a service account token.

        Prefer `token`: GF_SECURITY_ADMIN_PASSWORD only applies on first
        admin-user creation, so a stale/rotated secret can silently break
        basic auth on restart.

        `flag_prefix` names the CLI flags in auth error messages ("source" ->
        --source-token). `default_headers` are sent on every request; the import
        path uses that for provenance control, where forgetting the header on one
        of several call sites would be a silent behaviour change.
        """
        if not auth and not token:
            raise ValueError("GrafanaClient requires either auth=(user, password) or token=...")
        self.base_url = normalize_base_url(base_url, path_segment)
        self.timeout = timeout
        self.flag_prefix = flag_prefix
        self.session = session or requests.Session()
        if token:
            self.session.headers["Authorization"] = f"Bearer {token}"
        else:
            self.session.auth = auth
        if default_headers:
            self.session.headers.update(default_headers)
        retries = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            # POST is deliberately excluded: urllib3 cannot tell a request the
            # server never saw from one it processed before the 503, so retrying
            # a create would duplicate folders/rules/contact points.
            allowed_methods=("GET", "PUT"),
        )
        adapter = HTTPAdapter(max_retries=retries)
        self.session.mount("http://", adapter)
        self.session.mount("https://", adapter)

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[dict[str, Any]] = None,
        json_body: Any = None,
        headers: Optional[dict[str, str]] = None,
        timeout: Optional[float] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        try:
            resp = self.session.request(
                method,
                url,
                params=params,
                json=json_body,
                headers=headers,
                timeout=timeout or self.timeout,
            )
        except requests.RequestException as exc:
            raise GrafanaClientError(f"{method} {url} failed: {exc}", method=method, url=url) from exc
        return self._handle(method, url, resp)

    def _handle(self, method: str, url: str, resp: Any) -> Any:
        if resp.status_code == 401:
            raise GrafanaAuthError(
                f"{method} {url} returned 401 Unauthorized -- check --{self.flag_prefix}-token, or "
                f"--{self.flag_prefix}-user/--{self.flag_prefix}-password (note: the admin username "
                "may not be 'admin' -- check GF_SECURITY_ADMIN_USER on the pod; also note that "
                "Grafana does not reapply GF_SECURITY_ADMIN_PASSWORD to an already-existing admin "
                "user on restart, so a correct secret value can still 401 -- a service "
                "account token avoids this class of problem entirely)",
                method=method,
                url=url,
                status=401,
                body=resp.text[:500],
            )
        if resp.status_code == 403:
            raise GrafanaForbiddenError(
                f"{method} {url} returned 403 Forbidden -- the credentials are valid but lack "
                "permission for this endpoint. The provisioning API (alert rules, contact points, "
                "notification policy) needs a token whose org role is Admin; Viewer and Editor "
                "are not enough.",
                method=method,
                url=url,
                status=403,
                body=resp.text[:500],
            )
        if not resp.ok:
            error_class = _error_class_for_status(resp.status_code)
            raise error_class(
                f"{method} {url} returned {resp.status_code}: {resp.text[:500]}",
                method=method,
                url=url,
                status=resp.status_code,
                body=resp.text[:500],
            )

        # Some provisioning writes answer 200/202 with an empty body; that is a
        # success, not a decode error.
        if not (resp.content or b"").strip():
            return None
        try:
            return resp.json()
        except ValueError as exc:
            raise GrafanaClientError(
                f"{method} {url} returned {resp.status_code} with a non-JSON body: {resp.text[:500]}",
                method=method,
                url=url,
                status=resp.status_code,
                body=resp.text[:500],
            ) from exc

    def _get(self, path: str, **params: Any) -> Any:
        return self._request("GET", path, params=params)

    def search(self, limit: int = 5000) -> list[dict[str, Any]]:
        """Return every folder + dashboard row from /api/search."""
        return cast(list[dict[str, Any]], self._get("/api/search", limit=limit))

    def get_dashboard(self, uid: str) -> dict[str, Any]:
        """Return the full {"dashboard": ..., "meta": ...} payload for a uid."""
        return cast(dict[str, Any], self._get(f"/api/dashboards/uid/{uid}"))

    def list_alert_rules(self) -> list[dict[str, Any]]:
        """Return every provisioned alert rule (Grafana-managed unified alerting only)."""
        return cast(list[dict[str, Any]], self._get("/api/v1/provisioning/alert-rules"))

    def list_contact_points(self) -> list[dict[str, Any]]:
        """Return every provisioned contact point.

        Secure settings are redacted by Grafana itself; the response only
        marks which field names are set, via each receiver's `secureFields`.
        """
        return cast(list[dict[str, Any]], self._get("/api/v1/provisioning/contact-points"))

    def get_notification_policy_tree(self) -> dict[str, Any]:
        """Return the single root alertmanager route tree for this org."""
        return cast(dict[str, Any], self._get("/api/v1/provisioning/policies"))


def build_client(
    *,
    url: str,
    token: Optional[str] = None,
    user: Optional[str] = None,
    password: Optional[str] = None,
    path_segment: Optional[str] = None,
    flag_prefix: str = "source",
    default_headers: Optional[dict[str, str]] = None,
    timeout: float = 15.0,
    session: Optional[requests.Session] = None,
) -> GrafanaClient:
    """Build a client from CLI-shaped credential args, preferring a token.

    Shared by `export` (source instance) and `import --target api` (destination),
    so the token-beats-basic-auth precedence is decided in exactly one place.
    """
    if not token and not (user and password):
        raise ValueError(f"need --{flag_prefix}-token, or both --{flag_prefix}-user and --{flag_prefix}-password")
    return GrafanaClient(
        url,
        auth=None if token else (user or "", password or ""),
        token=token,
        path_segment=path_segment,
        timeout=timeout,
        session=session,
        flag_prefix=flag_prefix,
        default_headers=default_headers,
    )
