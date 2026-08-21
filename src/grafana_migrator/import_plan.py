"""Decide what a snapshot should add to a target, independently of how it lands.

This module owns every *skip* decision and every skip entry in the report; a
backend owns the created/failed entries. Keeping the split that way is what
makes `--target operator` and `--target api` agree on dedup semantics instead
of drifting: there is one implementation of "is this already there", and the
backends only differ in how they write what is genuinely new.

Nothing here knows about custom resources or HTTP. `ImportPlan` carries
source-side models plus the matched target objects, and a backend turns those
into whatever it writes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from .dedup import AlertRuleIndex, ContactPointIndex, DashboardIndex, FolderIndex
from .models import (
    ExistingAlertRuleGroup,
    ExistingContactPoint,
    ExistingDashboard,
    ExistingFolder,
    SourceAlertRule,
    SourceContactPoint,
    SourceDashboardRef,
    SourceFolder,
    SourceNotificationPolicy,
)
from .report import MigrationReport
from .source_dump import SourceDump, parse_alert_rule, parse_contact_point

logger = logging.getLogger(__name__)

_DEFAULT_POLICY_RECEIVER = "empty"


class IncompleteSnapshotError(RuntimeError):
    """search.json lists a dashboard the snapshot has no payload file for."""

    def __init__(self, uid: str) -> None:
        super().__init__(f"snapshot has no dashboards/{uid}.json")
        self.uid = uid


def is_default_contact_point(cp: SourceContactPoint) -> bool:
    """Grafana ships a no-op receiver named 'empty'; migrating it is pointless."""
    return cp.name == _DEFAULT_POLICY_RECEIVER and cp.type == _DEFAULT_POLICY_RECEIVER and not cp.settings


def is_default_notification_policy(policy: SourceNotificationPolicy) -> bool:
    """Whether this is Grafana's untouched default route tree.

    The default routes everything to the no-op 'empty' receiver and has no
    child routes, so there is no routing intent worth carrying over.
    """
    route = policy.route or {}
    if route.get("receiver") != _DEFAULT_POLICY_RECEIVER:
        return False
    return not route.get("routes")


@dataclass(frozen=True)
class PlanOptions:
    include_title_duplicates: bool = False
    skip_alerts: bool = False
    skip_notification_policy: bool = False


@dataclass(frozen=True)
class TargetInventory:
    """What already exists on the target, in backend-neutral form.

    `probe_notification_policy` is a callable rather than a value on purpose:
    answering it costs a request, and the planner only needs it when the source
    policy is non-default, so making it eager would add a call the kubectl path
    does not make today.
    """

    dashboards: list[ExistingDashboard] = field(default_factory=list)
    folders: list[ExistingFolder] = field(default_factory=list)
    alert_rule_groups: list[ExistingAlertRuleGroup] = field(default_factory=list)
    contact_points: list[ExistingContactPoint] = field(default_factory=list)
    probe_notification_policy: Callable[[], bool] = bool


@dataclass(frozen=True)
class RuleGroupUnit:
    """One (folder, rule group) pair -- the unit both backends write as a whole."""

    folder_uid: str
    folder_title: str
    rule_group: str
    rules: list[SourceAlertRule]


@dataclass
class ImportPlan:
    folders_new: list[SourceFolder] = field(default_factory=list)
    folders_existing: list[tuple[SourceFolder, ExistingFolder]] = field(default_factory=list)
    dashboards_new: list[tuple[SourceDashboardRef, dict[str, Any]]] = field(default_factory=list)
    rule_groups_new: list[RuleGroupUnit] = field(default_factory=list)
    contact_points_new: list[SourceContactPoint] = field(default_factory=list)
    notification_policy: Optional[SourceNotificationPolicy] = None


def source_folders(dump: SourceDump) -> list[SourceFolder]:
    return [SourceFolder(uid=i["uid"], title=i["title"]) for i in dump.search_results if i.get("type") == "dash-folder"]


def source_dashboards(dump: SourceDump) -> list[SourceDashboardRef]:
    return [
        SourceDashboardRef(
            uid=i["uid"],
            title=i["title"],
            folder_uid=i.get("folderUid"),
            folder_title=i.get("folderTitle") or None,
        )
        for i in dump.search_results
        if i.get("type") == "dash-db"
    ]


def plan_import(
    dump: SourceDump,
    inventory: TargetInventory,
    opts: PlanOptions,
    report: MigrationReport,
) -> ImportPlan:
    """Dedup `dump` against `inventory`, recording skips in `report`.

    Raises IncompleteSnapshotError if search.json references a dashboard whose
    payload file is missing -- that means a truncated snapshot, and continuing
    would silently migrate less than the operator asked for.
    """
    plan = ImportPlan()

    folders = source_folders(dump)
    dashboards = source_dashboards(dump)

    folder_index = FolderIndex(inventory.folders)
    for f in folders:
        existing = folder_index.find(f.title)
        if existing:
            plan.folders_existing.append((f, existing))
        else:
            plan.folders_new.append(f)

    dash_index = DashboardIndex(inventory.dashboards)
    for d in dashboards:
        decision = dash_index.decide(d.uid, d.title, include_title_duplicates=opts.include_title_duplicates)
        if decision.action == "skip_uid_match":
            report.skipped_uid_match.append(
                {"uid": d.uid, "title": d.title, "matched_cr_name": decision.matched_cr_name}
            )
            continue
        if decision.action == "skip_title_match":
            report.skipped_title_match.append(
                {"uid": d.uid, "title": d.title, "matched_cr_name": decision.matched_cr_name}
            )
            continue

        full = dump.dashboards_by_uid.get(d.uid)
        if full is None:
            raise IncompleteSnapshotError(d.uid)
        plan.dashboards_new.append((d, full))

    skip_alerts = opts.skip_alerts or dump.alert_rules_raw is None
    if skip_alerts:
        report.notification_policy_status = "skipped_by_flag"
        report.notification_policy_detail = "--skip-alerts was passed, or the snapshot has no alert data"
        return plan

    folder_title_by_uid = {f.uid: f.title for f in folders}

    rule_index = AlertRuleIndex(inventory.alert_rule_groups)
    units: dict[tuple[str, str], list[SourceAlertRule]] = {}
    for rule in [parse_alert_rule(r) for r in (dump.alert_rules_raw or [])]:
        decision = rule_index.decide(rule.uid, rule.title)
        if decision.action == "skip_uid_match":
            report.alert_rules_skipped_uid_match.append(
                {"uid": rule.uid, "title": rule.title, "matched_cr_name": decision.matched_cr_name}
            )
            continue
        units.setdefault((rule.folder_uid, rule.rule_group), []).append(rule)

    for (folder_uid, rule_group), rules in units.items():
        plan.rule_groups_new.append(
            RuleGroupUnit(
                folder_uid=folder_uid,
                folder_title=folder_title_by_uid.get(folder_uid, folder_uid),
                rule_group=rule_group,
                rules=rules,
            )
        )

    contact_point_index = ContactPointIndex(inventory.contact_points)
    for cp in [parse_contact_point(c) for c in (dump.contact_points_raw or [])]:
        if is_default_contact_point(cp):
            report.contact_points_skipped_default.append({"uid": cp.uid, "name": cp.name})
            continue
        decision = contact_point_index.decide(cp.name)
        if decision.action == "skip_name_match":
            report.contact_points_skipped_name_match.append(
                {"uid": cp.uid, "name": cp.name, "matched_cr_name": decision.matched_cr_name}
            )
            continue
        plan.contact_points_new.append(cp)

    _plan_notification_policy(dump, inventory, opts, report, plan)
    return plan


def _plan_notification_policy(
    dump: SourceDump,
    inventory: TargetInventory,
    opts: PlanOptions,
    report: MigrationReport,
    plan: ImportPlan,
) -> None:
    if opts.skip_notification_policy or dump.notification_policy_raw is None:
        report.notification_policy_status = (
            "skipped_by_flag" if opts.skip_notification_policy else "skipped_unavailable"
        )
        report.notification_policy_detail = (
            "--skip-notification-policy was passed"
            if opts.skip_notification_policy
            else "the source snapshot has no notification-policy.json (not fetched at export time)"
        )
        return

    policy = SourceNotificationPolicy(route=dump.notification_policy_raw or {})
    if is_default_notification_policy(policy):
        report.notification_policy_status = "skipped_default"
        report.notification_policy_detail = "source policy is Grafana's untouched default -- nothing to migrate"
        return
    if inventory.probe_notification_policy():
        report.notification_policy_status = "skipped_target_has_policy"
        report.notification_policy_detail = (
            "target namespace already has a GrafanaNotificationPolicy CR -- it represents the whole "
            "routing tree, so this tool will not risk clobbering it; merge manually if the source "
            "policy has routing worth carrying over"
        )
        return
    plan.notification_policy = policy
