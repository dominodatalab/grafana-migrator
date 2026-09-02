"""Push an ImportPlan straight into a target Grafana over HTTP.

The immediate half of the import, and the reason this is not a shared write
interface with operator_backend: every call here either succeeds or fails right
now, ordering is load-bearing because Grafana validates references, and a run
can end partway through with real state on the target.

Three rules shape the whole module:

- A 409/412 conflict is not a failure. It means the object is already there,
  which is the same answer dedup would have given with a fresher inventory --
  so it is recorded as a skip and the run continues. It is never retried with
  overwrite, because that would turn a safe re-run into a clobber.
- A 401/403, a transport error or a 5xx aborts. Twenty more identical 401s are
  noise, and a target that is gone will not come back mid-run.
- A dependency failure skips its dependents explicitly. If a folder cannot be
  created, its dashboards are reported as skipped, never quietly relocated to
  the General folder.

Nothing here logs a request body: contact point settings hold the values from
--secrets-file.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Mapping

from .api_payloads import alert_rule_body, contact_point_body, dashboard_body, folder_body, notification_policy_body
from .grafana_client import (
    GrafanaAuthError,
    GrafanaBadRequestError,
    GrafanaClient,
    GrafanaClientError,
    GrafanaConflictError,
    GrafanaForbiddenError,
    GrafanaNotFoundError,
)
from .import_plan import ImportPlan
from .report import MigrationReport
from .secrets_file import secrets_for

logger = logging.getLogger(__name__)

# Errors that are specific to the object being written: record and carry on.
_PER_OBJECT_ERRORS = (GrafanaBadRequestError, GrafanaNotFoundError)
# Errors that mean nothing else will work either.
_FATAL_ERRORS = (GrafanaAuthError, GrafanaForbiddenError)


class PushAborted(RuntimeError):
    """Raised internally to unwind when continuing would be pointless."""


@dataclass
class ApiPushOptions:
    dry_run: bool = False
    stop_on_first_error: bool = False
    secrets: Mapping[str, Mapping[str, str]] = field(default_factory=dict)


@dataclass
class _Outcome:
    """Per-kind bookkeeping, so the caller can report dependency skips."""

    failed_folder_source_uids: set[str] = field(default_factory=set)


def push(plan: ImportPlan, client: GrafanaClient, opts: ApiPushOptions, report: MigrationReport) -> int:
    """Write everything in `plan` to `client`. Returns a process exit code.

    Order is dependency-driven, with the cheap writes first so a bad
    --secrets-file surfaces before fifty dashboard POSTs: folders, contact
    points, dashboards, alert rules, then the notification policy last because
    Grafana validates that every receiver it names exists.
    """
    state = _Outcome()
    try:
        folder_uid_by_source_uid = _push_folders(plan, client, opts, report, state)
        _push_contact_points(plan, client, opts, report)
        _push_dashboards(plan, client, opts, report, folder_uid_by_source_uid, state)
        _push_alert_rules(plan, client, opts, report, folder_uid_by_source_uid, state)
        _push_notification_policy(plan, client, opts, report)
    except PushAborted:
        logger.error("aborting: the target rejected the run, see the failures in the report")

    return 1 if report.failures else 0


# ---------------------------------------------------------------------------
# error handling
# ---------------------------------------------------------------------------


def _record_failure(
    report: MigrationReport, opts: ApiPushOptions, kind: str, identity: dict[str, Any], exc: GrafanaClientError
) -> None:
    report.failures.append(
        {
            "kind": kind,
            "identity": identity,
            "phase": "create",
            "status": exc.status,
            "error": str(exc),
        }
    )
    if opts.stop_on_first_error:
        raise PushAborted from exc


def _handle(
    report: MigrationReport,
    opts: ApiPushOptions,
    kind: str,
    identity: dict[str, Any],
    exc: GrafanaClientError,
) -> str:
    """Classify a write error. Returns "conflict" or "failed"; may abort."""
    if isinstance(exc, GrafanaConflictError):
        return "conflict"
    if isinstance(exc, _FATAL_ERRORS):
        _record_failure(report, opts, kind, identity, exc)
        raise PushAborted from exc
    if isinstance(exc, _PER_OBJECT_ERRORS):
        _record_failure(report, opts, kind, identity, exc)
        return "failed"
    # Transport error or a 5xx that survived the retry policy: the target is
    # gone or unhealthy, so the remaining writes would fail the same way.
    _record_failure(report, opts, kind, identity, exc)
    raise PushAborted from exc


def _would(kind: str, identity: str) -> None:
    # Identity only -- never the body.
    logger.info("would create %s %s", kind, identity)


# ---------------------------------------------------------------------------
# per-kind pushes
# ---------------------------------------------------------------------------


def _push_folders(
    plan: ImportPlan,
    client: GrafanaClient,
    opts: ApiPushOptions,
    report: MigrationReport,
    state: _Outcome,
) -> dict[str, str]:
    """Create missing folders, preserving the source uid.

    Preserving the uid is what lets dashboards and rules reference the folder
    without a lookup, and what makes a second run converge by identity.
    """
    uid_by_source_uid: dict[str, str] = {}

    for source_folder, existing in plan.folders_existing:
        target_uid = existing.uid or existing.cr_name
        uid_by_source_uid[source_folder.uid] = target_uid
        report.folders_reused.append({"title": source_folder.title, "target_ref": target_uid})

    for f in plan.folders_new:
        if opts.dry_run:
            _would("folder", f"uid={f.uid} title={f.title!r}")
            uid_by_source_uid[f.uid] = f.uid
            report.folders_created.append({"title": f.title, "target_ref": f.uid})
            continue
        try:
            client.create_folder(folder_body(f.title, uid=f.uid))
        except GrafanaClientError as exc:
            outcome = _handle(report, opts, "folder", {"uid": f.uid, "title": f.title}, exc)
            if outcome == "conflict":
                # Already there: usable as a parent, same as a dedup match.
                uid_by_source_uid[f.uid] = f.uid
                report.folders_reused.append(
                    {"title": f.title, "target_ref": f.uid, "action": "already_exists_on_push"}
                )
            else:
                state.failed_folder_source_uids.add(f.uid)
            continue
        uid_by_source_uid[f.uid] = f.uid
        report.folders_created.append({"title": f.title, "target_ref": f.uid})

    return uid_by_source_uid


def _push_contact_points(
    plan: ImportPlan, client: GrafanaClient, opts: ApiPushOptions, report: MigrationReport
) -> None:
    """Create receivers before the rules and policy that name them.

    Also the cheapest writes, so a --secrets-file mistake shows up early.
    """
    for cp in plan.contact_points_new:
        built = contact_point_body(cp, secrets=secrets_for(opts.secrets, cp.name))
        if built.secure_fields_missing:
            report.warnings.append(
                f"contact point {cp.name!r} created without {', '.join(built.secure_fields_missing)} "
                "-- the integration stays disabled until the value is set (see --secrets-file)"
            )
        entry = {
            "uid": cp.uid,
            "name": cp.name,
            "type": cp.type,
            "target_ref": cp.uid,
            "secret_name": None,
            "secure_field_names": list(cp.secure_field_names),
            "secure_fields_supplied": list(built.secure_fields_supplied),
            "secure_fields_missing": list(built.secure_fields_missing),
        }
        if opts.dry_run:
            _would("contact point", f"name={cp.name!r} type={cp.type}")
            report.contact_points_migrated.append(entry)
            continue
        try:
            client.create_contact_point(built.body)
        except GrafanaClientError as exc:
            outcome = _handle(report, opts, "contact_point", {"uid": cp.uid, "name": cp.name}, exc)
            if outcome == "conflict":
                report.contact_points_skipped_name_match.append(
                    {"uid": cp.uid, "name": cp.name, "matched_ref": cp.uid, "action": "already_exists_on_push"}
                )
            continue
        report.contact_points_migrated.append(entry)


def _push_dashboards(
    plan: ImportPlan,
    client: GrafanaClient,
    opts: ApiPushOptions,
    report: MigrationReport,
    folder_uid_by_source_uid: dict[str, str],
    state: _Outcome,
) -> None:
    for d, snapshot_payload in plan.dashboards_new:
        if d.folder_uid and d.folder_uid in state.failed_folder_source_uids:
            # Do not fall back to General: silently relocating a dashboard is
            # worse than not migrating it.
            report.skipped_dependency_failed.append(
                {
                    "uid": d.uid,
                    "title": d.title,
                    "detail": f"folder {d.folder_uid} could not be created",
                }
            )
            continue

        folder_uid = folder_uid_by_source_uid.get(d.folder_uid) if d.folder_uid else None
        entry = {"uid": d.uid, "title": d.title, "target_ref": d.uid}
        if opts.dry_run:
            _would("dashboard", f"uid={d.uid} title={d.title!r} folder={folder_uid}")
            report.migrated.append(entry)
            continue
        try:
            client.create_dashboard(dashboard_body(snapshot_payload, folder_uid=folder_uid))
        except GrafanaClientError as exc:
            outcome = _handle(report, opts, "dashboard", {"uid": d.uid, "title": d.title}, exc)
            if outcome == "conflict":
                report.skipped_uid_match.append(
                    {"uid": d.uid, "title": d.title, "matched_ref": d.uid, "action": "already_exists_on_push"}
                )
            continue
        report.migrated.append(entry)


def _push_alert_rules(
    plan: ImportPlan,
    client: GrafanaClient,
    opts: ApiPushOptions,
    report: MigrationReport,
    folder_uid_by_source_uid: dict[str, str],
    state: _Outcome,
) -> None:
    """Create rules individually -- Grafana has no bulk rule-group endpoint.

    Rules come after contact points because notification_settings.receiver is
    validated against existing receivers.
    """
    for unit in plan.rule_groups_new:
        if unit.folder_uid in state.failed_folder_source_uids:
            for rule in unit.rules:
                report.alert_rules_skipped_dependency_failed.append(
                    {
                        "uid": rule.uid,
                        "title": rule.title,
                        "detail": f"folder {unit.folder_uid} could not be created",
                    }
                )
            continue

        folder_uid = folder_uid_by_source_uid.get(unit.folder_uid, unit.folder_uid)
        target_ref = f"{folder_uid}/{unit.rule_group}"
        for rule in unit.rules:
            entry = {
                "uid": rule.uid,
                "title": rule.title,
                "rule_group": unit.rule_group,
                "target_ref": target_ref,
            }
            if opts.dry_run:
                _would("alert rule", f"uid={rule.uid} group={unit.rule_group!r} folder={folder_uid}")
                report.alert_rules_migrated.append(entry)
                continue
            try:
                client.create_alert_rule(alert_rule_body(rule, folder_uid=folder_uid))
            except GrafanaClientError as exc:
                outcome = _handle(report, opts, "alert_rule", {"uid": rule.uid, "title": rule.title}, exc)
                if outcome == "conflict":
                    report.alert_rules_skipped_uid_match.append(
                        {
                            "uid": rule.uid,
                            "title": rule.title,
                            "matched_ref": rule.uid,
                            "action": "already_exists_on_push",
                        }
                    )
                continue
            report.alert_rules_migrated.append(entry)


def _push_notification_policy(
    plan: ImportPlan, client: GrafanaClient, opts: ApiPushOptions, report: MigrationReport
) -> None:
    """Last: the PUT validates that every receiver the tree names exists."""
    policy = plan.notification_policy
    if policy is None:
        return
    if opts.dry_run:
        logger.info("would replace the notification policy tree")
        report.notification_policy_status = "migrated"
        report.notification_policy_detail = "would replace the target's route tree"
        return
    try:
        client.put_notification_policy(notification_policy_body(policy))
    except GrafanaClientError as exc:
        report.notification_policy_status = "failed"
        report.notification_policy_detail = str(exc)
        _record_failure(report, opts, "notification_policy", {"name": "route tree"}, exc)
        return
    report.notification_policy_status = "migrated"
    report.notification_policy_detail = "replaced the target's route tree"
