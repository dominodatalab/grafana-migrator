"""Read/write a raw snapshot of a source Grafana instance's content.

Lets `export` (source only) and `import` (dedup + manifests against a
target) run as fully independent steps, so a snapshot can be captured before
a target exists and reused across multiple targets.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .grafana_client import GrafanaClient, GrafanaClientError

logger = logging.getLogger(__name__)

_META_FILENAME = "meta.json"
_SEARCH_FILENAME = "search.json"
_ALERT_RULES_FILENAME = "alert-rules.json"
_CONTACT_POINTS_FILENAME = "contact-points.json"
_NOTIFICATION_POLICY_FILENAME = "notification-policy.json"
_DASHBOARDS_DIRNAME = "dashboards"


class SourceDumpError(RuntimeError):
    """Raised when a previously-written source snapshot can't be read back."""


@dataclass
class SourceDump:
    """Every raw API response this tool needs, either freshly fetched or read
    back from a previously-written snapshot directory.

    `alert_rules_raw`/`contact_points_raw`/`notification_policy_raw` are
    `None` when that category was never fetched (`--skip-alerts` at export
    time, or an older snapshot), as distinct from an empty list/dict meaning
    "fetched, and there was nothing there."
    """

    search_results: list[dict[str, Any]]
    dashboards_by_uid: dict[str, dict[str, Any]]
    alert_rules_raw: Optional[list[dict[str, Any]]]
    contact_points_raw: Optional[list[dict[str, Any]]]
    notification_policy_raw: Optional[dict[str, Any]]


def fetch_source(client: GrafanaClient, *, skip_alerts: bool, skip_notification_policy: bool) -> SourceDump:
    """Pull every resource this tool cares about from the source instance's HTTP API."""
    search_results = client.search()
    dashboard_uids = [i["uid"] for i in search_results if i.get("type") == "dash-db"]
    dashboards_by_uid = {uid: client.get_dashboard(uid) for uid in dashboard_uids}

    alert_rules_raw: Optional[list[dict[str, Any]]] = None
    contact_points_raw: Optional[list[dict[str, Any]]] = None
    notification_policy_raw: Optional[dict[str, Any]] = None

    if not skip_alerts:
        try:
            alert_rules_raw = client.list_alert_rules()
        except GrafanaClientError as exc:
            logger.warning("could not fetch alert rules: %s", exc)
        try:
            contact_points_raw = client.list_contact_points()
        except GrafanaClientError as exc:
            logger.warning("could not fetch contact points: %s", exc)
        if not skip_notification_policy:
            try:
                notification_policy_raw = client.get_notification_policy_tree()
            except GrafanaClientError as exc:
                logger.warning("could not fetch notification policy: %s", exc)

    return SourceDump(
        search_results=search_results,
        dashboards_by_uid=dashboards_by_uid,
        alert_rules_raw=alert_rules_raw,
        contact_points_raw=contact_points_raw,
        notification_policy_raw=notification_policy_raw,
    )


def _dump_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def write_source_dump(dump: SourceDump, output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _dump_json(output_dir / _SEARCH_FILENAME, dump.search_results)

    dash_dir = output_dir / _DASHBOARDS_DIRNAME
    dash_dir.mkdir(exist_ok=True)
    for uid, payload in dump.dashboards_by_uid.items():
        _dump_json(dash_dir / f"{uid}.json", payload)

    if dump.alert_rules_raw is not None:
        _dump_json(output_dir / _ALERT_RULES_FILENAME, dump.alert_rules_raw)
    if dump.contact_points_raw is not None:
        _dump_json(output_dir / _CONTACT_POINTS_FILENAME, dump.contact_points_raw)
    if dump.notification_policy_raw is not None:
        _dump_json(output_dir / _NOTIFICATION_POLICY_FILENAME, dump.notification_policy_raw)

    _dump_json(
        output_dir / _META_FILENAME,
        {
            "dashboard_count": len(dump.dashboards_by_uid),
            "alerts_fetched": dump.alert_rules_raw is not None,
            "notification_policy_fetched": dump.notification_policy_raw is not None,
        },
    )


def read_source_dump(export_dir: Path) -> SourceDump:
    search_path = export_dir / _SEARCH_FILENAME
    if not search_path.is_file():
        raise SourceDumpError(
            f"{export_dir} doesn't look like a `grafana-migrator export` directory " f"(missing {_SEARCH_FILENAME})"
        )
    search_results = json.loads(search_path.read_text())

    dashboards_by_uid: dict[str, dict[str, Any]] = {}
    dash_dir = export_dir / _DASHBOARDS_DIRNAME
    if dash_dir.is_dir():
        for f in sorted(dash_dir.glob("*.json")):
            dashboards_by_uid[f.stem] = json.loads(f.read_text())

    def _load_optional(name: str) -> Any:
        p = export_dir / name
        return json.loads(p.read_text()) if p.is_file() else None

    return SourceDump(
        search_results=search_results,
        dashboards_by_uid=dashboards_by_uid,
        alert_rules_raw=_load_optional(_ALERT_RULES_FILENAME),
        contact_points_raw=_load_optional(_CONTACT_POINTS_FILENAME),
        notification_policy_raw=_load_optional(_NOTIFICATION_POLICY_FILENAME),
    )
