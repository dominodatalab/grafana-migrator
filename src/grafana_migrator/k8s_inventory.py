"""Read existing GrafanaDashboard / GrafanaFolder CRs from the target cluster.

Shells out to `kubectl get ... -o json` rather than a k8s client library --
this module only ever reads CRs to build a dedup index; writes go through
plain YAML files applied via `kubectl apply`.
"""

from __future__ import annotations

import json
import logging
import subprocess
from typing import Any, Optional

from .models import ExistingAlertRuleGroup, ExistingContactPoint, ExistingDashboard, ExistingFolder

logger = logging.getLogger(__name__)

_DASHBOARD_CRD = "grafanadashboards.grafana.integreatly.org"
_FOLDER_CRD = "grafanafolders.grafana.integreatly.org"
_ALERT_RULE_GROUP_CRD = "grafanaalertrulegroups.grafana.integreatly.org"
_CONTACT_POINT_CRD = "grafanacontactpoints.grafana.integreatly.org"
_NOTIFICATION_POLICY_CRD = "grafananotificationpolicies.grafana.integreatly.org"


class KubectlError(RuntimeError):
    """Raised when `kubectl` itself fails (not found, bad context, RBAC, etc.)."""


def _kubectl_get_json(resource: str, namespace: str, context: Optional[str]) -> dict[str, Any]:
    cmd = ["kubectl", "get", resource, "-n", namespace, "-o", "json"]
    if context:
        cmd += ["--context", context]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
    except FileNotFoundError as exc:
        raise KubectlError("kubectl not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise KubectlError(f"kubectl get {resource} timed out after 60s") from exc

    if proc.returncode != 0:
        raise KubectlError(f"kubectl get {resource} -n {namespace} failed: {proc.stderr.strip()}")
    try:
        parsed: dict[str, Any] = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise KubectlError(f"kubectl get {resource} returned non-JSON output: {exc}") from exc
    return parsed


def list_existing_dashboards(namespace: str, context: Optional[str] = None) -> list[ExistingDashboard]:
    data = _kubectl_get_json(_DASHBOARD_CRD, namespace, context)
    out: list[ExistingDashboard] = []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        spec = item.get("spec", {})
        uid = None
        title = None
        raw_json = spec.get("json")
        if raw_json:
            try:
                parsed = json.loads(raw_json)
                uid = parsed.get("uid")
                title = parsed.get("title")
            except json.JSONDecodeError:
                logger.warning("GrafanaDashboard %s has non-JSON spec.json, skipping content parse", name)
        out.append(ExistingDashboard(cr_name=name, namespace=namespace, uid=uid, title=title))
    return out


def list_existing_folders(namespace: str, context: Optional[str] = None) -> list[ExistingFolder]:
    data = _kubectl_get_json(_FOLDER_CRD, namespace, context)
    out: list[ExistingFolder] = []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        title = item.get("spec", {}).get("title", name)
        out.append(ExistingFolder(cr_name=name, namespace=namespace, title=title))
    return out


def list_existing_alert_rule_groups(
    namespace: str, context: Optional[str] = None
) -> list[ExistingAlertRuleGroup]:
    data = _kubectl_get_json(_ALERT_RULE_GROUP_CRD, namespace, context)
    out: list[ExistingAlertRuleGroup] = []
    for item in data.get("items", []):
        name = item["metadata"]["name"]
        spec = item.get("spec", {})
        rule_uids = tuple(r["uid"] for r in spec.get("rules", []) if r.get("uid"))
        out.append(
            ExistingAlertRuleGroup(
                cr_name=name,
                namespace=namespace,
                folder_ref=spec.get("folderRef") or spec.get("folderUID"),
                rule_group=spec.get("name"),
                rule_uids=rule_uids,
            )
        )
    return out


def list_existing_contact_points(namespace: str, context: Optional[str] = None) -> list[ExistingContactPoint]:
    data = _kubectl_get_json(_CONTACT_POINT_CRD, namespace, context)
    out: list[ExistingContactPoint] = []
    for item in data.get("items", []):
        cr_name = item["metadata"]["name"]
        name = item.get("spec", {}).get("name", cr_name)
        out.append(ExistingContactPoint(cr_name=cr_name, namespace=namespace, name=name))
    return out


def has_existing_notification_policy(namespace: str, context: Optional[str] = None) -> bool:
    """Whether the target namespace already has any GrafanaNotificationPolicy CR.

    The CR represents the entire alertmanager route tree -- normally only
    one exists. Used as a guardrail against generating a competing one, not
    as a dedup signal.
    """
    data = _kubectl_get_json(_NOTIFICATION_POLICY_CRD, namespace, context)
    return len(data.get("items", [])) > 0
