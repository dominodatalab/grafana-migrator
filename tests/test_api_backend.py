"""Pushing a plan over HTTP: ordering, conflicts, failures, dependency skips.

The assertions that matter most here are about call *sequence* and about which
calls do not happen, which is why these run against an ordered call log rather
than a URL-matching mock.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
import requests
from fake_session import FakeResponse, FakeSession

from grafana_migrator.api_backend import ApiPushOptions, push
from grafana_migrator.grafana_client import GrafanaClient
from grafana_migrator.import_plan import ImportPlan, RuleGroupUnit
from grafana_migrator.models import (
    ExistingFolder,
    SourceContactPoint,
    SourceDashboardRef,
    SourceFolder,
    SourceNotificationPolicy,
)
from grafana_migrator.report import MigrationReport
from grafana_migrator.source_dump import parse_alert_rule

FOLDERS = "/api/folders"
DASHBOARDS = "/api/dashboards/db"
RULES = "/api/v1/provisioning/alert-rules"
CONTACT_POINTS = "/api/v1/provisioning/contact-points"
POLICIES = "/api/v1/provisioning/policies"

OK = FakeResponse(200, {"ok": True})

RULE = parse_alert_rule(
    {"uid": "r-1", "title": "Hot", "ruleGroup": "G", "folderUID": "f-1", "condition": "A", "data": []}
)


def _plan(**kw: Any) -> ImportPlan:
    defaults: dict[str, Any] = dict(
        folders_new=[SourceFolder(uid="f-1", title="Platform Team")],
        folders_existing=[],
        dashboards_new=[
            (
                SourceDashboardRef(uid="d-1", title="CPU", folder_uid="f-1", folder_title="Platform Team"),
                {"dashboard": {"uid": "d-1", "title": "CPU"}, "meta": {}},
            )
        ],
        rule_groups_new=[RuleGroupUnit(folder_uid="f-1", folder_title="Platform Team", rule_group="G", rules=[RULE])],
        contact_points_new=[
            SourceContactPoint(uid="cp-1", name="pd", type="pagerduty", settings={}, secure_field_names=("key",))
        ],
        notification_policy=SourceNotificationPolicy(route={"receiver": "pd"}),
    )
    defaults.update(kw)
    return ImportPlan(**defaults)


def _client(routes: dict | None = None) -> GrafanaClient:
    base = {
        ("POST", FOLDERS): OK,
        ("POST", DASHBOARDS): OK,
        ("POST", RULES): OK,
        ("POST", CONTACT_POINTS): OK,
        ("PUT", POLICIES): OK,
    }
    base.update(routes or {})
    return GrafanaClient("http://graf.test", token="t", session=cast(requests.Session, FakeSession(base)))


def _calls(client: GrafanaClient) -> list[tuple[str, str]]:
    return [(m, p) for m, p, _, _ in client.session.calls]  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# ordering
# ---------------------------------------------------------------------------


def test_push_order_is_dependency_driven():
    # Folders before anything referencing them; contact points before the rules
    # and policy that name them; the whole-tree policy PUT last.
    client = _client()
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 0
    assert _calls(client) == [
        ("POST", FOLDERS),
        ("POST", CONTACT_POINTS),
        ("POST", DASHBOARDS),
        ("POST", RULES),
        ("PUT", POLICIES),
    ]


def test_existing_folder_is_reused_without_a_write():
    client = _client()
    report = MigrationReport(backend="api")
    plan = _plan(
        folders_new=[],
        folders_existing=[
            (
                SourceFolder(uid="f-1", title="Platform Team"),
                ExistingFolder(cr_name="f-1", namespace="", title="Platform Team", uid="f-1"),
            )
        ],
    )
    push(plan, client, ApiPushOptions(), report)
    assert ("POST", FOLDERS) not in _calls(client)
    assert report.folders_reused[0]["target_ref"] == "f-1"


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_issues_no_write_requests_at_all():
    client = _client()
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(dry_run=True), report) == 0
    assert _calls(client) == []


def test_dry_run_still_reports_what_would_happen_with_real_refs():
    report = MigrationReport(backend="api")
    push(_plan(), _client(), ApiPushOptions(dry_run=True), report)
    assert [m["target_ref"] for m in report.migrated] == ["d-1"]
    assert [f["target_ref"] for f in report.folders_created] == ["f-1"]
    assert report.notification_policy_status == "migrated"


def test_dry_run_still_warns_about_missing_secure_fields():
    # The single most useful thing a dry run can tell you.
    report = MigrationReport(backend="api")
    push(_plan(), _client(), ApiPushOptions(dry_run=True), report)
    assert any("stays disabled" in w for w in report.warnings)


# ---------------------------------------------------------------------------
# conflicts are skips, not failures
# ---------------------------------------------------------------------------


def test_dashboard_conflict_is_recorded_as_a_skip_and_the_run_succeeds():
    client = _client({("POST", DASHBOARDS): FakeResponse(412, {"message": "name-exists"})})
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 0
    assert report.failures == []
    assert report.migrated == []
    assert report.skipped_uid_match[0]["action"] == "already_exists_on_push"


def test_folder_conflict_still_lets_its_dashboards_through():
    # The folder is already there, so it is usable as a parent.
    client = _client({("POST", FOLDERS): FakeResponse(409, {"message": "exists"})})
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 0
    assert ("POST", DASHBOARDS) in _calls(client)
    assert report.folders_reused[0]["action"] == "already_exists_on_push"


def test_contact_point_and_rule_conflicts_are_skips():
    client = _client(
        {
            ("POST", CONTACT_POINTS): FakeResponse(409, {"message": "exists"}),
            ("POST", RULES): FakeResponse(409, {"message": "exists"}),
        }
    )
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 0
    assert report.contact_points_skipped_name_match[0]["action"] == "already_exists_on_push"
    assert report.alert_rules_skipped_uid_match[0]["action"] == "already_exists_on_push"


def test_a_conflict_is_never_retried_with_overwrite():
    client = _client({("POST", DASHBOARDS): FakeResponse(412, {"message": "name-exists"})})
    push(_plan(), client, ApiPushOptions(), MigrationReport(backend="api"))
    assert _calls(client).count(("POST", DASHBOARDS)) == 1


# ---------------------------------------------------------------------------
# per-object failures
# ---------------------------------------------------------------------------


def test_a_bad_request_fails_that_object_and_continues():
    client = _client({("POST", DASHBOARDS): FakeResponse(400, {"message": "bad panel"})})
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 1
    assert len(report.failures) == 1
    assert report.failures[0]["kind"] == "dashboard"
    assert report.failures[0]["status"] == 400
    # later kinds still ran
    assert ("POST", RULES) in _calls(client)
    assert ("PUT", POLICIES) in _calls(client)


def test_one_failure_among_several_dashboards_leaves_the_others_pushed():
    dashboards = [
        (
            SourceDashboardRef(uid=f"d-{i}", title=f"D{i}", folder_uid=None, folder_title=None),
            {"dashboard": {"uid": f"d-{i}"}, "meta": {}},
        )
        for i in range(1, 4)
    ]
    client = _client({("POST", DASHBOARDS): [OK, FakeResponse(400, {"message": "nope"}), OK]})
    report = MigrationReport(backend="api")
    rc = push(_plan(dashboards_new=dashboards), client, ApiPushOptions(), report)
    assert rc == 1
    assert [m["uid"] for m in report.migrated] == ["d-1", "d-3"]
    assert len(report.failures) == 1


def test_stop_on_first_error_aborts_the_rest():
    client = _client({("POST", DASHBOARDS): FakeResponse(400, {"message": "nope"})})
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(stop_on_first_error=True), report) == 1
    assert ("POST", RULES) not in _calls(client)
    assert ("PUT", POLICIES) not in _calls(client)


# ---------------------------------------------------------------------------
# fatal errors abort
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [401, 403])
def test_auth_and_permission_errors_abort_immediately(status):
    client = _client({("POST", FOLDERS): FakeResponse(status, {"message": "no"})})
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 1
    # nothing after the folder was attempted
    assert _calls(client) == [("POST", FOLDERS)]


def test_a_server_error_aborts_rather_than_grinding_through():
    client = _client({("POST", FOLDERS): FakeResponse(500, {"message": "boom"})})
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 1
    assert _calls(client) == [("POST", FOLDERS)]


def test_a_transport_error_aborts():
    client = _client({("POST", FOLDERS): requests.ConnectionError("refused")})
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 1
    assert len(report.failures) == 1


# ---------------------------------------------------------------------------
# dependency skips
# ---------------------------------------------------------------------------


def test_a_failed_folder_skips_its_dashboards_rather_than_relocating_them():
    # Silently putting them in General would be worse than not migrating.
    client = _client({("POST", FOLDERS): FakeResponse(400, {"message": "bad title"})})
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 1
    assert ("POST", DASHBOARDS) not in _calls(client)
    assert report.migrated == []
    assert report.skipped_title_match == []
    skipped = report.skipped_dependency_failed[0]
    assert "could not be created" in skipped["detail"]


def test_a_failed_folder_skips_its_alert_rules_too():
    client = _client({("POST", FOLDERS): FakeResponse(400, {"message": "bad title"})})
    report = MigrationReport(backend="api")
    push(_plan(), client, ApiPushOptions(), report)
    assert ("POST", RULES) not in _calls(client)
    assert report.alert_rules_skipped_uid_match == []
    assert "could not be created" in report.alert_rules_skipped_dependency_failed[0]["detail"]


def test_a_dashboard_with_no_folder_is_unaffected_by_a_folder_failure():
    rootless = (
        SourceDashboardRef(uid="d-9", title="Rootless", folder_uid=None, folder_title=None),
        {"dashboard": {"uid": "d-9"}, "meta": {}},
    )
    client = _client({("POST", FOLDERS): FakeResponse(400, {"message": "bad"})})
    report = MigrationReport(backend="api")
    push(_plan(dashboards_new=[rootless]), client, ApiPushOptions(), report)
    assert [m["uid"] for m in report.migrated] == ["d-9"]


# ---------------------------------------------------------------------------
# secrets and policy
# ---------------------------------------------------------------------------


def test_supplied_secrets_reach_the_request_and_are_reported():
    client = _client()
    report = MigrationReport(backend="api")
    push(_plan(), client, ApiPushOptions(secrets={"pd": {"key": "s3cret"}}), report)
    body = next(b for m, pth, _, b in client.session.calls if pth == CONTACT_POINTS)  # type: ignore[attr-defined]
    assert body["settings"]["key"] == "s3cret"
    assert report.contact_points_migrated[0]["secure_fields_supplied"] == ["key"]
    assert report.warnings == []


def test_a_missing_secret_omits_the_field_and_warns():
    client = _client()
    report = MigrationReport(backend="api")
    push(_plan(), client, ApiPushOptions(), report)
    body = next(b for m, pth, _, b in client.session.calls if pth == CONTACT_POINTS)  # type: ignore[attr-defined]
    assert "key" not in body["settings"]
    assert report.contact_points_migrated[0]["secure_fields_missing"] == ["key"]
    assert any("stays disabled" in w for w in report.warnings)


def test_no_policy_in_the_plan_means_no_put():
    client = _client()
    push(_plan(notification_policy=None), client, ApiPushOptions(), MigrationReport(backend="api"))
    assert ("PUT", POLICIES) not in _calls(client)


def test_a_rejected_policy_put_is_reported_as_failed():
    client = _client({("PUT", POLICIES): FakeResponse(400, {"message": "unknown receiver"})})
    report = MigrationReport(backend="api")
    assert push(_plan(), client, ApiPushOptions(), report) == 1
    assert report.notification_policy_status == "failed"
    assert any(f["kind"] == "notification_policy" for f in report.failures)


def test_secrets_are_never_written_to_the_log(caplog):
    caplog.set_level("DEBUG")
    push(_plan(), _client(), ApiPushOptions(secrets={"pd": {"key": "s3cret"}}), MigrationReport(backend="api"))
    assert "s3cret" not in caplog.text


def test_dry_run_log_names_objects_but_no_bodies(caplog):
    caplog.set_level("INFO")
    push(
        _plan(),
        _client(),
        ApiPushOptions(dry_run=True, secrets={"pd": {"key": "s3cret"}}),
        MigrationReport(backend="api"),
    )
    assert "would create dashboard" in caplog.text
    assert "s3cret" not in caplog.text
