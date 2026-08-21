"""Turn source Grafana JSON into GrafanaDashboard/GrafanaFolder/alerting CR manifests."""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .models import SourceAlertRule, SourceContactPoint, SourceNotificationPolicy

# Matches Alertmanager matcher syntax, e.g. `team="x"`, `severity!=critical`,
# `env=~prod.*`: <name><op><value>, value optionally quoted.
_MATCHER_STRING = re.compile(r'^\s*([^\s!=~]+)\s*(=~|!~|!=|=)\s*(.*)\s*$')

MANAGED_BY_LABEL = {"app.kubernetes.io/managed-by": "grafana-migrator"}

# Grafana's factory-default no-op receiver/policy -- nothing to migrate.
_DEFAULT_POLICY_RECEIVER = "empty"


def dashboard_json_to_manifest(
    dashboard_json: dict[str, Any],
    *,
    name: str,
    namespace: str,
    instance_selector: dict[str, str],
    source_uid: str,
    source_title: str,
    folder_ref: Optional[str] = None,
    folder: Optional[str] = None,
) -> dict[str, Any]:
    """Build a GrafanaDashboard manifest for one exported dashboard.

    Strips `id`/`version` (source-instance internal, meaningless on the
    target); keeps `uid` so the dashboard's identity carries over.
    """
    cleaned = dict(dashboard_json)
    cleaned.pop("id", None)
    cleaned.pop("version", None)

    spec: dict[str, Any] = {
        "allowCrossNamespaceImport": False,
        "instanceSelector": {"matchLabels": dict(instance_selector)},
        "json": json.dumps(cleaned, indent=2, sort_keys=True) + "\n",
    }
    if folder_ref:
        spec["folderRef"] = folder_ref
    else:
        spec["folder"] = folder or "General"

    return {
        "apiVersion": "grafana.integreatly.org/v1beta1",
        "kind": "GrafanaDashboard",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(MANAGED_BY_LABEL),
            "annotations": {
                "grafana-migrator/source-uid": source_uid,
                "grafana-migrator/source-title": source_title,
            },
        },
        "spec": spec,
    }


def alert_rule_to_spec(rule: SourceAlertRule) -> dict[str, Any]:
    """Build one `spec.rules[]` entry for a GrafanaAlertRuleGroup CR."""
    spec: dict[str, Any] = {
        "uid": rule.uid,
        "title": rule.title,
        "condition": rule.condition,
        "data": rule.data,
        "noDataState": rule.no_data_state,
        "execErrState": rule.exec_err_state,
        "for": rule.for_,
    }
    if rule.annotations:
        spec["annotations"] = dict(rule.annotations)
    if rule.labels:
        spec["labels"] = dict(rule.labels)
    if rule.is_paused:
        spec["isPaused"] = True
    if rule.notification_settings:
        spec["notificationSettings"] = dict(rule.notification_settings)
    if rule.dashboard_uid:
        spec["dashboardUid"] = rule.dashboard_uid
    if rule.panel_id is not None:
        spec["panelId"] = rule.panel_id
    if rule.record:
        spec["record"] = dict(rule.record)
    if rule.keep_firing_for:
        spec["keepFiringFor"] = rule.keep_firing_for
    return spec


def alert_rule_group_to_manifest(
    rules: list[SourceAlertRule],
    *,
    name: str,
    namespace: str,
    instance_selector: dict[str, str],
    rule_group: str,
    interval: str = "1m",
    folder_ref: Optional[str] = None,
    folder_uid: Optional[str] = None,
) -> dict[str, Any]:
    """Build a GrafanaAlertRuleGroup manifest for one (folder, rule_group) unit.

    Exactly one of folder_ref/folder_uid should be set.
    """
    spec: dict[str, Any] = {
        "instanceSelector": {"matchLabels": dict(instance_selector)},
        "interval": interval,
        "name": rule_group,
        "rules": [alert_rule_to_spec(r) for r in rules],
    }
    if folder_ref:
        spec["folderRef"] = folder_ref
    elif folder_uid:
        spec["folderUID"] = folder_uid

    return {
        "apiVersion": "grafana.integreatly.org/v1beta1",
        "kind": "GrafanaAlertRuleGroup",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(MANAGED_BY_LABEL),
            "annotations": {
                "grafana-migrator/source-rule-group": rule_group,
                "grafana-migrator/source-rule-uids": ",".join(r.uid for r in rules),
            },
        },
        "spec": spec,
    }


def is_default_contact_point(cp: SourceContactPoint) -> bool:
    """Whether this is just Grafana's built-in no-op receiver, not customer config."""
    return cp.name == _DEFAULT_POLICY_RECEIVER and cp.type == _DEFAULT_POLICY_RECEIVER and not cp.settings


def contact_point_to_manifest(
    cp: SourceContactPoint,
    *,
    name: str,
    namespace: str,
    instance_selector: dict[str, str],
    secret_name: str,
) -> dict[str, Any]:
    """Build a GrafanaContactPoint manifest.

    Secure fields are never available from the source API (Grafana redacts
    them). Each one gets a `valuesFrom.secretKeyRef` pointing at
    `secret_name` instead of a value; the integration stays disabled until
    that Secret is populated with the real value.
    """
    receiver: dict[str, Any] = {
        "uid": cp.uid,
        "type": cp.type,
        "settings": dict(cp.settings),
    }
    if cp.disable_resolve_message:
        receiver["disableResolveMessage"] = True
    if cp.secure_field_names:
        receiver["valuesFrom"] = [
            {
                "targetPath": field_name,
                "valueFrom": {"secretKeyRef": {"name": secret_name, "key": field_name}},
            }
            for field_name in cp.secure_field_names
        ]

    return {
        "apiVersion": "grafana.integreatly.org/v1beta1",
        "kind": "GrafanaContactPoint",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(MANAGED_BY_LABEL),
            "annotations": {"grafana-migrator/source-uid": cp.uid},
        },
        "spec": {
            "instanceSelector": {"matchLabels": dict(instance_selector)},
            "name": cp.name,
            "receivers": [receiver],
        },
    }


def is_default_notification_policy(policy: SourceNotificationPolicy) -> bool:
    """Whether the source's root route is still Grafana's untouched factory default."""
    route = policy.route
    return route.get("receiver") == _DEFAULT_POLICY_RECEIVER and not route.get("routes")


def _parse_matcher_triple(raw: str) -> list[str]:
    """Convert one Alertmanager-syntax matcher string into an
    `[name, operator, value]` triple for the CRD's `object_matchers` field.
    """
    match = _MATCHER_STRING.match(raw)
    if not match:
        raise ValueError(f"cannot parse notification policy matcher {raw!r}")
    name, op, value = match.groups()
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        value = value[1:-1]
    return [name, op, value]


def _normalize_route(route: dict[str, Any]) -> dict[str, Any]:
    """Recursively convert a route's string-syntax `matchers` into the
    `object_matchers` triple form the CRD expects.

    The CRD's structured `matchers` field ({name, value, isEqual, isRegex})
    looks like the natural target, but grafana-operator 5.24.0 fails to
    translate it back into a request Grafana's policy API accepts (400
    putPolicyTreeBadRequest, confirmed live). `object_matchers` triples
    round-trip correctly instead.
    """
    route = dict(route)
    if route.get("matchers"):
        route["object_matchers"] = [
            _parse_matcher_triple(m) if isinstance(m, str) else m for m in route.pop("matchers")
        ]
    if route.get("routes"):
        route["routes"] = [_normalize_route(r) for r in route["routes"]]
    return route


def notification_policy_to_manifest(
    policy: SourceNotificationPolicy,
    *,
    name: str,
    namespace: str,
    instance_selector: dict[str, str],
) -> dict[str, Any]:
    """Build a GrafanaNotificationPolicy manifest from the source's whole route tree.

    Callers MUST only do this when the target has no existing
    GrafanaNotificationPolicy CR (see k8s_inventory.has_existing_notification_policy)
    -- this CR represents the entire routing tree, so generating one over
    existing custom routing would silently replace it rather than merge.
    """
    route = _normalize_route(policy.route)
    route.pop("provenance", None)

    return {
        "apiVersion": "grafana.integreatly.org/v1beta1",
        "kind": "GrafanaNotificationPolicy",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(MANAGED_BY_LABEL),
        },
        "spec": {
            "instanceSelector": {"matchLabels": dict(instance_selector)},
            "route": route,
        },
    }


def folder_title_to_manifest(
    title: str,
    *,
    name: str,
    namespace: str,
    instance_selector: dict[str, str],
    source_uid: str,
) -> dict[str, Any]:
    return {
        "apiVersion": "grafana.integreatly.org/v1beta1",
        "kind": "GrafanaFolder",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": dict(MANAGED_BY_LABEL),
            "annotations": {"grafana-migrator/source-uid": source_uid},
        },
        "spec": {
            "allowCrossNamespaceImport": False,
            "instanceSelector": {"matchLabels": dict(instance_selector)},
            "title": title,
        },
    }
