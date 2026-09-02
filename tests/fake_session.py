"""Minimal stand-in for requests.Session, injected via GrafanaClient(session=...).

Chosen over an HTTP-mocking library because this repo declares no dev
dependencies, and because the assertions that matter most for the import path
are about call *ordering* and about writes never happening under --dry-run --
both of which an ordered call log expresses directly. Patching at the adapter
layer would also drag the retry/mount config into the test surface; here it is
inert.
"""

from __future__ import annotations

import json as _json
from typing import Any, Optional
from urllib.parse import urlsplit


class FakeResponse:
    def __init__(self, status_code: int = 200, body: Any = None, text: Optional[str] = None) -> None:
        self.status_code = status_code
        if text is not None:
            self._text = text
        elif body is None:
            self._text = ""
        else:
            self._text = _json.dumps(body)

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400

    @property
    def content(self) -> bytes:
        return self._text.encode()

    @property
    def text(self) -> str:
        return self._text

    def json(self) -> Any:
        return _json.loads(self._text)


class FakeSession:
    """Routes are keyed (METHOD, path). A list value is consumed in order."""

    def __init__(self, routes: Optional[dict[tuple[str, str], Any]] = None) -> None:
        self.headers: dict[str, str] = {}
        self.auth = None
        self.routes = dict(routes or {})
        self.calls: list[tuple[str, str, Optional[dict], Any]] = []
        self.mounted: list[str] = []

    def mount(self, prefix: str, adapter: Any) -> None:
        self.mounted.append(prefix)

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[dict] = None,
        json: Any = None,
        headers: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> FakeResponse:
        path = urlsplit(url).path
        self.calls.append((method, path, params, json))
        entry = self.routes.get((method, path))
        if entry is None:
            return FakeResponse(404, {"message": f"no fake route for {method} {path}"})
        if isinstance(entry, list):
            entry = entry.pop(0) if entry else FakeResponse(500, {"message": "fake routes exhausted"})
        if isinstance(entry, Exception):
            raise entry
        return entry

    def get(self, url: str, **kw: Any) -> FakeResponse:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw: Any) -> FakeResponse:
        return self.request("POST", url, **kw)

    def put(self, url: str, **kw: Any) -> FakeResponse:
        return self.request("PUT", url, **kw)

    def paths(self, method: Optional[str] = None) -> list[str]:
        return [p for m, p, _, _ in self.calls if method is None or m == method]

    def bodies(self, method: Optional[str] = None) -> list[Any]:
        return [b for m, _, _, b in self.calls if method is None or m == method]
