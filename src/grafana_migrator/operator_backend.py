"""Turn an ImportPlan into Grafana Operator CR manifests on disk.

The deferred half of the import: this backend produces YAML and hands it to
`kubectl apply`, so it never learns whether a write actually succeeded. That
asymmetry with the immediate, per-object api backend is why the two share a
plan rather than a write interface.

Report ownership: the planner records skips, this module records what it
created or reused.
"""

from __future__ import annotations

import logging
from typing import Any

from .import_plan import ImportPlan
from .naming import alert_rule_group_cr_name, contact_point_cr_name, dashboard_cr_name, folder_cr_name
from .report import MigrationReport
from .transform import (
    alert_rule_group_to_manifest,
    contact_point_to_manifest,
    dashboard_json_to_manifest,
    folder_title_to_manifest,
    notification_policy_to_manifest,
)

logger = logging.getLogger(__name__)

NOTIFICATION_POLICY_CR_NAME = "migrated-notification-policy"

Manifest = tuple[str, dict[str, Any]]


def _placeholder_secret(secret_name: str, namespace: str, cp_type: str, field_names: tuple[str, ...]) -> dict[str, Any]:
    """An empty Secret for a contact point's redacted fields.

    Grafana redacts secure settings on every read, so the real values were
    never in the snapshot; a human fills this in before applying.
    """
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            "annotations": {
                "grafana-migrator/note": (
                    f"placeholder -- populate with the real {cp_type} credentials "
                    "before applying the matching GrafanaContactPoint"
                )
            },
        },
        "type": "Opaque",
        "stringData": {field_name: "" for field_name in field_names},
    }


def emit_manifests(
    plan: ImportPlan,
    *,
    namespace: str,
    instance_selector: dict[str, str],
    report: MigrationReport,
) -> list[Manifest]:
    manifests: list[Manifest] = []

    # Folders first: dashboards and rule groups reference them by CR name, and
    # that name is only knowable once reuse-vs-create is settled.
    folder_ref_by_source_uid: dict[str, str] = {}
    for f, existing in plan.folders_existing:
        folder_ref_by_source_uid[f.uid] = existing.cr_name
        report.folders_reused.append({"title": f.title, "cr_name": existing.cr_name})
    for f in plan.folders_new:
        cr_name = folder_cr_name(f.title)
        folder_ref_by_source_uid[f.uid] = cr_name
        manifests.append(
            (
                f"folders/{cr_name}.yaml",
                folder_title_to_manifest(
                    f.title,
                    name=cr_name,
                    namespace=namespace,
                    instance_selector=instance_selector,
                    source_uid=f.uid,
                ),
            )
        )
        report.folders_created.append({"title": f.title, "cr_name": cr_name})

    for d, full in plan.dashboards_new:
        cr_name = dashboard_cr_name(d.uid)
        folder_ref = folder_ref_by_source_uid.get(d.folder_uid) if d.folder_uid else None
        manifests.append(
            (
                f"dashboards/{cr_name}.yaml",
                dashboard_json_to_manifest(
                    full["dashboard"],
                    name=cr_name,
                    namespace=namespace,
                    instance_selector=instance_selector,
                    source_uid=d.uid,
                    source_title=d.title,
                    folder_ref=folder_ref,
                    folder=None if folder_ref else "General",
                ),
            )
        )
        report.migrated.append({"uid": d.uid, "title": d.title, "cr_name": cr_name})

    for unit in plan.rule_groups_new:
        cr_name = alert_rule_group_cr_name(unit.folder_title, unit.rule_group)
        folder_ref = folder_ref_by_source_uid.get(unit.folder_uid)
        manifests.append(
            (
                f"alert-rules/{cr_name}.yaml",
                alert_rule_group_to_manifest(
                    unit.rules,
                    name=cr_name,
                    namespace=namespace,
                    instance_selector=instance_selector,
                    rule_group=unit.rule_group,
                    folder_ref=folder_ref,
                    folder_uid=None if folder_ref else unit.folder_uid,
                ),
            )
        )
        for rule in unit.rules:
            report.alert_rules_migrated.append(
                {"uid": rule.uid, "title": rule.title, "rule_group": unit.rule_group, "cr_name": cr_name}
            )

    for cp in plan.contact_points_new:
        cr_name = contact_point_cr_name(cp.name)
        secret_name = f"{cr_name}-secrets"
        manifests.append(
            (
                f"contact-points/{cr_name}.yaml",
                contact_point_to_manifest(
                    cp,
                    name=cr_name,
                    namespace=namespace,
                    instance_selector=instance_selector,
                    secret_name=secret_name,
                ),
            )
        )
        if cp.secure_field_names:
            manifests.append(
                (
                    f"contact-points/{secret_name}.yaml",
                    _placeholder_secret(secret_name, namespace, cp.type, cp.secure_field_names),
                )
            )
        report.contact_points_migrated.append(
            {
                "uid": cp.uid,
                "name": cp.name,
                "type": cp.type,
                "cr_name": cr_name,
                "secret_name": secret_name if cp.secure_field_names else None,
                "secure_field_names": list(cp.secure_field_names),
            }
        )

    if plan.notification_policy is not None:
        cr_name = NOTIFICATION_POLICY_CR_NAME
        manifests.append(
            (
                f"notification-policy/{cr_name}.yaml",
                notification_policy_to_manifest(
                    plan.notification_policy,
                    name=cr_name,
                    namespace=namespace,
                    instance_selector=instance_selector,
                ),
            )
        )
        report.notification_policy_status = "migrated"
        report.notification_policy_detail = f"-> {cr_name}"

    return manifests
