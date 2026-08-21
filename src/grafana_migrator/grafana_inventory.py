"""Read a target Grafana instance's existing content over HTTP.

The HTTP counterpart to k8s_inventory: same TargetInventory out, so dedup.py
and the planner are reused unchanged and both backends agree on what "already
there" means.

Two differences from the kubectl side are worth knowing about:

- One /api/search call yields both the dashboard and folder inventories, so
  this needs 4 requests where the operator path makes 5 kubectl calls.
- Grafana returns *every* dashboard, not just the ones an operator manages, so
  title-collision skips will fire more often here than in operator mode.
  That is correct and conservative, but it makes --include-title-duplicates a
  much more commonly needed flag.
"""

from __future__ import annotations

import logging
from typing import Any

from .grafana_client import GrafanaClient
from .import_plan import (
    POLICY_CUSTOM,
    POLICY_DEFAULT,
    POLICY_PROVISIONED,
    TargetInventory,
    is_default_notification_policy,
)
from .models import (
    ExistingAlertRuleGroup,
    ExistingContactPoint,
    ExistingDashboard,
    ExistingFolder,
    SourceNotificationPolicy,
)

logger = logging.getLogger(__name__)


def list_existing_dashboards_and_folders(
    client: GrafanaClient,
) -> tuple[list[ExistingDashboard], list[ExistingFolder]]:
    """Split one /api/search response into the dashboard and folder inventories.

    `cr_name` carries the Grafana uid here -- it is the opaque "what is this
    called on the target" field, which is a CR name only in operator mode.
    """
    dashboards: list[ExistingDashboard] = []
    folders: list[ExistingFolder] = []
    for row in client.search():
        uid = row.get("uid")
        title = row.get("title")
        if not uid:
            continue
        if row.get("type") == "dash-db":
            dashboards.append(ExistingDashboard(cr_name=uid, namespace="", uid=uid, title=title))
        elif row.get("type") == "dash-folder":
            folders.append(ExistingFolder(cr_name=uid, namespace="", title=title or uid, uid=uid))
    return dashboards, folders


def list_existing_alert_rule_groups(client: GrafanaClient) -> list[ExistingAlertRuleGroup]:
    """Group the flat provisioning rule list into (folder, group) units.

    Grafana has no "rule group" object to GET, so the grouping is derived --
    which matches how the operator CRs are shaped and keeps AlertRuleIndex
    (which keys on individual rule uids) working unchanged.
    """
    grouped: dict[tuple[str, str], list[str]] = {}
    for rule in client.list_alert_rules():
        folder_uid = rule.get("folderUID") or ""
        rule_group = rule.get("ruleGroup") or ""
        uid = rule.get("uid")
        if uid:
            grouped.setdefault((folder_uid, rule_group), []).append(uid)
    return [
        ExistingAlertRuleGroup(
            cr_name=f"{folder_uid}/{rule_group}",
            namespace="",
            folder_ref=folder_uid,
            rule_group=rule_group,
            rule_uids=tuple(uids),
        )
        for (folder_uid, rule_group), uids in grouped.items()
    ]


def list_existing_contact_points(client: GrafanaClient) -> list[ExistingContactPoint]:
    out: list[ExistingContactPoint] = []
    for cp in client.list_contact_points():
        name = cp.get("name")
        if name:
            out.append(ExistingContactPoint(cr_name=cp.get("uid") or name, namespace="", name=name))
    return out


def notification_policy_state(client: GrafanaClient) -> str:
    """Classify the target's route tree.

    Unlike the CRD, there is no "absent" here: Grafana always returns a tree.
    A non-empty `provenance` means something else manages it, so writing would
    be rejected or reverted -- distinct from a hand-edited tree we simply
    refuse to clobber.
    """
    tree: dict[str, Any] = client.get_notification_policy_tree() or {}
    if tree.get("provenance"):
        return POLICY_PROVISIONED
    if is_default_notification_policy(SourceNotificationPolicy(route=tree)):
        return POLICY_DEFAULT
    return POLICY_CUSTOM


def build_target_inventory(client: GrafanaClient, *, include_alerting: bool) -> TargetInventory:
    """Read the target Grafana into the backend-neutral TargetInventory.

    Mirrors k8s_inventory.build_target_inventory, including keeping the policy
    check behind a callable so a run whose source policy is default never pays
    for it.
    """
    dashboards, folders = list_existing_dashboards_and_folders(client)
    logger.info(
        "discovered %d existing dashboard(s) and %d existing folder(s) on the target Grafana",
        len(dashboards),
        len(folders),
    )

    rule_groups: list[ExistingAlertRuleGroup] = []
    contact_points: list[ExistingContactPoint] = []
    if include_alerting:
        rule_groups = list_existing_alert_rule_groups(client)
        contact_points = list_existing_contact_points(client)
        logger.info(
            "discovered %d existing alert rule group(s) and %d existing contact point(s) on the target Grafana",
            len(rule_groups),
            len(contact_points),
        )

    return TargetInventory(
        dashboards=dashboards,
        folders=folders,
        alert_rule_groups=rule_groups,
        contact_points=contact_points,
        probe_notification_policy_state=lambda: notification_policy_state(client),
    )
