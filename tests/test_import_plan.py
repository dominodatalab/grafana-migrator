"""Planner coverage: the skip decisions both backends share.

These tests are deliberately backend-free -- they assert on the plan and the
report, never on manifests or HTTP. That is the property that keeps
`--target operator` and `--target api` agreeing about what is already there.
"""

from __future__ import annotations

from typing import Any

import pytest

from grafana_migrator.import_plan import (
    POLICY_ABSENT,
    POLICY_CUSTOM,
    POLICY_DEFAULT,
    POLICY_PROVISIONED,
    IncompleteSnapshotError,
    PlanOptions,
    TargetInventory,
    is_default_contact_point,
    is_default_notification_policy,
    plan_import,
)
from grafana_migrator.models import (
    ExistingAlertRuleGroup,
    ExistingContactPoint,
    ExistingDashboard,
    ExistingFolder,
    SourceContactPoint,
    SourceNotificationPolicy,
)
from grafana_migrator.report import MigrationReport
from grafana_migrator.source_dump import SourceDump

DEFAULT_CONTACT_POINT = SourceContactPoint(uid="default-uid", name="empty", type="empty", settings={})
SLACK_CONTACT_POINT = SourceContactPoint(
    uid="slack-uid", name="Platform Slack", type="slack", settings={"recipient": "#platform-alerts"}
)
DEFAULT_POLICY = SourceNotificationPolicy(route={"receiver": "empty", "group_by": ["grafana_folder", "alertname"]})
CUSTOM_POLICY = SourceNotificationPolicy(
    route={
        "receiver": "empty",
        "group_by": ["grafana_folder", "alertname"],
        "routes": [{"receiver": "Platform Slack", "matchers": ["severity=critical"]}],
    }
)


def test_is_default_contact_point_detects_grafanas_builtin_receiver():
    assert is_default_contact_point(DEFAULT_CONTACT_POINT) is True
    assert is_default_contact_point(SLACK_CONTACT_POINT) is False


def test_is_default_notification_policy_detects_factory_default():
    assert is_default_notification_policy(DEFAULT_POLICY) is True
    assert is_default_notification_policy(CUSTOM_POLICY) is False


def _dump(**overrides: Any) -> SourceDump:
    fields: dict[str, Any] = dict(
        search_results=[
            {"uid": "f-1", "title": "Platform Team", "type": "dash-folder"},
            {"uid": "d-1", "title": "CPU", "type": "dash-db", "folderUid": "f-1"},
            {"uid": "d-2", "title": "Memory", "type": "dash-db"},
        ],
        dashboards_by_uid={
            "d-1": {"dashboard": {"uid": "d-1", "title": "CPU"}, "meta": {}},
            "d-2": {"dashboard": {"uid": "d-2", "title": "Memory"}, "meta": {}},
        },
        alert_rules_raw=[
            {
                "uid": "r-1",
                "title": "Hot",
                "ruleGroup": "G",
                "folderUID": "f-1",
                "condition": "A",
                "data": [],
            }
        ],
        contact_points_raw=[
            {"uid": "cp-1", "name": "Platform Slack", "type": "slack", "settings": {"recipient": "#x"}},
            {"uid": "cp-0", "name": "empty", "type": "empty", "settings": {}},
        ],
        notification_policy_raw={"receiver": "Platform Slack", "routes": [{"receiver": "Platform Slack"}]},
    )
    fields.update(overrides)
    return SourceDump(**fields)


def _plan(inventory=None, **opt_kw):
    report = MigrationReport()
    plan = plan_import(_dump(), inventory or TargetInventory(), PlanOptions(**opt_kw), report)
    return plan, report


def test_empty_target_plans_everything_as_new():
    plan, report = _plan()
    assert [f.uid for f in plan.folders_new] == ["f-1"]
    assert plan.folders_existing == []
    assert [d.uid for d, _ in plan.dashboards_new] == ["d-1", "d-2"]
    assert [u.rule_group for u in plan.rule_groups_new] == ["G"]
    assert [cp.name for cp in plan.contact_points_new] == ["Platform Slack"]
    assert plan.notification_policy is not None
    # the factory-default receiver is dropped before any target lookup
    assert [c["name"] for c in report.contact_points_skipped_default] == ["empty"]


def test_uid_match_is_skipped_not_planned():
    inv = TargetInventory(dashboards=[ExistingDashboard(cr_name="existing", namespace="ns", uid="d-1", title="CPU")])
    plan, report = _plan(inv)
    assert [d.uid for d, _ in plan.dashboards_new] == ["d-2"]
    assert [s["uid"] for s in report.skipped_uid_match] == ["d-1"]


def test_title_match_under_a_different_uid_is_skipped_unless_opted_in():
    inv = TargetInventory(
        dashboards=[ExistingDashboard(cr_name="other", namespace="ns", uid="totally-different", title="CPU")]
    )
    plan, report = _plan(inv)
    assert [d.uid for d, _ in plan.dashboards_new] == ["d-2"]
    assert [s["uid"] for s in report.skipped_title_match] == ["d-1"]

    plan, report = _plan(inv, include_title_duplicates=True)
    assert [d.uid for d, _ in plan.dashboards_new] == ["d-1", "d-2"]
    assert report.skipped_title_match == []


def test_existing_folder_is_matched_for_reuse_not_recreated():
    inv = TargetInventory(folders=[ExistingFolder(cr_name="preexisting", namespace="ns", title="platform team")])
    plan, _ = _plan(inv)
    assert plan.folders_new == []
    assert [(f.uid, e.cr_name) for f, e in plan.folders_existing] == [("f-1", "preexisting")]


def test_rule_already_on_target_by_uid_is_skipped():
    inv = TargetInventory(
        alert_rule_groups=[
            ExistingAlertRuleGroup(cr_name="grp", namespace="ns", folder_ref="f", rule_group="G", rule_uids=("r-1",))
        ]
    )
    plan, report = _plan(inv)
    assert plan.rule_groups_new == []
    assert [s["uid"] for s in report.alert_rules_skipped_uid_match] == ["r-1"]


def test_contact_point_matching_by_name_is_skipped():
    inv = TargetInventory(contact_points=[ExistingContactPoint(cr_name="cp", namespace="ns", name="platform slack")])
    plan, report = _plan(inv)
    assert plan.contact_points_new == []
    assert [s["name"] for s in report.contact_points_skipped_name_match] == ["Platform Slack"]


def test_skip_alerts_stops_before_any_alerting_decision():
    plan, report = _plan(skip_alerts=True)
    assert plan.rule_groups_new == []
    assert plan.contact_points_new == []
    assert plan.notification_policy is None
    assert report.notification_policy_status == "skipped_by_flag"
    # dashboards and folders are unaffected
    assert len(plan.dashboards_new) == 2


def test_default_source_policy_is_not_migrated():
    report = MigrationReport()
    dump = _dump(notification_policy_raw={"receiver": "empty"})
    plan = plan_import(dump, TargetInventory(), PlanOptions(), report)
    assert plan.notification_policy is None
    assert report.notification_policy_status == "skipped_default"


def test_policy_is_not_migrated_when_the_target_already_has_a_custom_one():
    inv = TargetInventory(probe_notification_policy_state=lambda: POLICY_CUSTOM)
    plan, report = _plan(inv)
    assert plan.notification_policy is None
    assert report.notification_policy_status == "skipped_target_has_policy"


def test_policy_is_migrated_when_the_target_tree_is_still_the_factory_default():
    inv = TargetInventory(probe_notification_policy_state=lambda: POLICY_DEFAULT)
    plan, report = _plan(inv)
    assert plan.notification_policy is not None


def test_provisioned_target_policy_is_reported_distinctly_from_merely_custom():
    # A provisioned tree would reject the write or revert it, which is a
    # different remedy than "merge it yourself".
    inv = TargetInventory(probe_notification_policy_state=lambda: POLICY_PROVISIONED)
    plan, report = _plan(inv)
    assert plan.notification_policy is None
    assert report.notification_policy_status == "skipped_target_policy_provisioned"


def test_policy_probe_is_not_called_when_the_source_policy_is_default():
    # Probing costs a request, so it must stay lazy.
    calls = []

    def probe():
        calls.append(1)
        return POLICY_ABSENT

    dump = _dump(notification_policy_raw={"receiver": "empty"})
    plan_import(dump, TargetInventory(probe_notification_policy_state=probe), PlanOptions(), MigrationReport())
    assert calls == []


def test_snapshot_missing_a_dashboard_payload_is_an_error():
    dump = _dump(dashboards_by_uid={"d-1": {"dashboard": {}, "meta": {}}})
    with pytest.raises(IncompleteSnapshotError) as excinfo:
        plan_import(dump, TargetInventory(), PlanOptions(), MigrationReport())
    assert excinfo.value.uid == "d-2"
