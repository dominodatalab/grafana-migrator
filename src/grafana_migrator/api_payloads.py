"""Build Grafana HTTP API request bodies from a snapshot.

The deliberate twin of transform.py: same inputs, different output shape. They
are not unified because most of transform.py has no counterpart here -- the
camelCase remapping, the folderRef indirection and the object_matchers rewrite
all exist to satisfy the operator's CRDs, and pushing straight to Grafana
skips them entirely.

Everything here is a pure function. No requests, no client, no I/O -- so the
decisions that are easy to get wrong (what to strip, what to omit, what to
leave verbatim) are settled and tested before any write code exists.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

from .models import SourceAlertRule, SourceContactPoint, SourceNotificationPolicy

# Server-assigned or instance-local, so replaying them is either meaningless or
# actively wrong on a different instance.
_ALERT_RULE_SERVER_FIELDS = ("id", "orgID", "updated", "provenance")

# Recording rules do not evaluate a condition, and Grafana returns these three
# as empty strings for them. Sending "" is worse than omitting the key: the
# enum validation rejects it.
_ALERT_RULE_EVAL_FIELDS = ("condition", "noDataState", "execErrState")


def folder_body(title: str, *, uid: Optional[str] = None, parent_uid: Optional[str] = None) -> dict[str, Any]:
    """POST /api/folders.

    Passing the source `uid` through is what lets a re-run converge by
    identity, and lets dashboards reference the folder without a name lookup --
    neither of which the CR path can do, since the CR does not carry a uid.
    """
    body: dict[str, Any] = {"title": title}
    if uid:
        body["uid"] = uid
    if parent_uid:
        body["parentUid"] = parent_uid
    return body


def dashboard_body(
    snapshot_payload: Mapping[str, Any],
    *,
    folder_uid: Optional[str] = None,
    overwrite: bool = False,
    message: Optional[str] = None,
) -> dict[str, Any]:
    """POST /api/dashboards/db, from a snapshot's dashboards/<uid>.json.

    `id` and `version` are instance-local: a stale `id` makes Grafana treat the
    request as an update to whatever holds that id on the target, and a
    `version` it does not recognise trips optimistic locking. `uid` and `title`
    are kept -- the uid is the idempotency key.
    """
    dashboard = dict(snapshot_payload.get("dashboard") or {})
    dashboard.pop("id", None)
    dashboard.pop("version", None)
    body: dict[str, Any] = {"dashboard": dashboard, "overwrite": overwrite}
    if folder_uid:
        body["folderUid"] = folder_uid
    if message:
        body["message"] = message
    return body


def alert_rule_body(rule: SourceAlertRule, *, folder_uid: Optional[str] = None) -> dict[str, Any]:
    """POST /api/v1/provisioning/alert-rules.

    Replays the captured payload rather than rebuilding it from parsed fields,
    minus the server-assigned keys. `folder_uid` overrides folderUID for the
    case where the folder was matched to an existing target folder under a
    different uid.
    """
    body = {k: v for k, v in rule.raw.items() if k not in _ALERT_RULE_SERVER_FIELDS}
    if folder_uid:
        body["folderUID"] = folder_uid
    if body.get("record"):
        for key in _ALERT_RULE_EVAL_FIELDS:
            if not body.get(key):
                body.pop(key, None)
    return body


@dataclass(frozen=True)
class ContactPointBody:
    """A contact point request plus which secure fields actually got values.

    The caller needs the two lists for the report: creating a receiver whose
    credential is missing leaves a silently dead integration, so it has to be
    said out loud rather than inferred from a 200.
    """

    body: dict[str, Any]
    secure_fields_supplied: tuple[str, ...]
    secure_fields_missing: tuple[str, ...]


def contact_point_body(
    cp: SourceContactPoint,
    *,
    secrets: Optional[Mapping[str, str]] = None,
) -> ContactPointBody:
    """POST /api/v1/provisioning/contact-points.

    There is no secretKeyRef indirection over HTTP, so secure values go inline
    or not at all. Unsupplied fields are *omitted* rather than sent empty: for
    several integration types Grafana validates the field, and "" either 400s
    or wipes a value that was already there.
    """
    supplied_values = secrets or {}
    settings = dict(cp.settings)
    supplied: list[str] = []
    missing: list[str] = []
    for field_name in cp.secure_field_names:
        value = supplied_values.get(field_name)
        if value:
            settings[field_name] = value
            supplied.append(field_name)
        else:
            missing.append(field_name)

    body: dict[str, Any] = {"name": cp.name, "type": cp.type, "settings": settings}
    if cp.uid:
        body["uid"] = cp.uid
    if cp.disable_resolve_message:
        body["disableResolveMessage"] = True
    return ContactPointBody(body=body, secure_fields_supplied=tuple(supplied), secure_fields_missing=tuple(missing))


def _strip_provenance(node: Mapping[str, Any]) -> dict[str, Any]:
    out = {k: v for k, v in node.items() if k != "provenance"}
    child_routes = out.get("routes")
    if isinstance(child_routes, list):
        out["routes"] = [_strip_provenance(r) if isinstance(r, Mapping) else r for r in child_routes]
    return out


def notification_policy_body(policy: SourceNotificationPolicy) -> dict[str, Any]:
    """PUT /api/v1/provisioning/policies -- replaces the entire tree.

    The captured tree goes back verbatim apart from `provenance`, which is the
    server's own bookkeeping and is rejected on write. Matchers are left
    exactly as Grafana returned them, in whichever form: the object_matchers
    rewrite in transform.py is a grafana-operator workaround, and doing it here
    would corrupt a payload that already round-trips by construction.
    """
    return _strip_provenance(policy.route or {})
