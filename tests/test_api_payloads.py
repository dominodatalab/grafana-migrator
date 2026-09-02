"""Request-body construction. Pure functions -- no client, no network.

These pin the decisions that are easy to get wrong and expensive to debug
against a live instance: what gets stripped, what gets omitted rather than
emptied, and what must go back verbatim.
"""

from __future__ import annotations

from grafana_migrator.api_payloads import (
    alert_rule_body,
    contact_point_body,
    dashboard_body,
    folder_body,
    notification_policy_body,
)
from grafana_migrator.models import SourceContactPoint, SourceNotificationPolicy
from grafana_migrator.source_dump import parse_alert_rule

SNAPSHOT_DASHBOARD = {
    "dashboard": {"uid": "d-1", "title": "CPU", "id": 42, "version": 7, "panels": [{"id": 1}]},
    "meta": {"folderUid": "f-1", "folderTitle": "Platform Team", "version": 7},
}

API_RULE = {
    "id": 11,
    "orgID": 1,
    "uid": "r-1",
    "title": "High CPU",
    "ruleGroup": "Platform",
    "folderUID": "f-1",
    "condition": "B",
    "data": [{"refId": "A", "datasourceUid": "prom"}],
    "noDataState": "NoData",
    "execErrState": "Alerting",
    "for": "5m",
    "keep_firing_for": "0s",
    "annotations": {"summary": "hot"},
    "labels": {"severity": "critical"},
    "isPaused": False,
    "notification_settings": {"receiver": "pd"},
    "updated": "2024-01-01T00:00:00Z",
    "provenance": "file",
}


# ---------------------------------------------------------------------------
# folders
# ---------------------------------------------------------------------------


def test_folder_body_preserves_the_source_uid():
    # Preserving the uid is what makes a re-run converge by identity.
    assert folder_body("Platform Team", uid="f-1") == {"title": "Platform Team", "uid": "f-1"}


def test_folder_body_omits_uid_and_parent_when_not_given():
    assert folder_body("Platform Team") == {"title": "Platform Team"}


def test_folder_body_carries_parent_uid_when_nesting_is_known():
    assert folder_body("Child", uid="c", parent_uid="p")["parentUid"] == "p"


# ---------------------------------------------------------------------------
# dashboards
# ---------------------------------------------------------------------------


def test_dashboard_body_strips_instance_local_id_and_version():
    body = dashboard_body(SNAPSHOT_DASHBOARD, folder_uid="f-1")
    assert "id" not in body["dashboard"]
    assert "version" not in body["dashboard"]


def test_dashboard_body_keeps_uid_title_and_panels():
    body = dashboard_body(SNAPSHOT_DASHBOARD)
    assert body["dashboard"]["uid"] == "d-1"
    assert body["dashboard"]["title"] == "CPU"
    assert body["dashboard"]["panels"] == [{"id": 1}]


def test_dashboard_body_defaults_to_not_overwriting():
    # The skip-on-conflict policy depends on this being false.
    assert dashboard_body(SNAPSHOT_DASHBOARD)["overwrite"] is False


def test_dashboard_body_sets_folder_uid_only_when_given():
    assert dashboard_body(SNAPSHOT_DASHBOARD, folder_uid="f-1")["folderUid"] == "f-1"
    assert "folderUid" not in dashboard_body(SNAPSHOT_DASHBOARD)


def test_dashboard_body_does_not_mutate_the_snapshot():
    dashboard_body(SNAPSHOT_DASHBOARD)
    assert SNAPSHOT_DASHBOARD["dashboard"]["id"] == 42


def test_dashboard_body_survives_an_empty_payload():
    assert dashboard_body({})["dashboard"] == {}


# ---------------------------------------------------------------------------
# alert rules
# ---------------------------------------------------------------------------


def test_alert_rule_body_strips_server_assigned_fields():
    body = alert_rule_body(parse_alert_rule(API_RULE))
    for key in ("id", "orgID", "updated", "provenance"):
        assert key not in body


def test_alert_rule_body_replays_the_captured_payload_verbatim_otherwise():
    body = alert_rule_body(parse_alert_rule(API_RULE))
    assert body["uid"] == "r-1"
    assert body["ruleGroup"] == "Platform"
    assert body["data"] == [{"refId": "A", "datasourceUid": "prom"}]
    # snake_case fields stay snake_case -- this endpoint is where they came from
    assert body["notification_settings"] == {"receiver": "pd"}
    assert body["keep_firing_for"] == "0s"
    assert body["for"] == "5m"


def test_alert_rule_body_can_retarget_the_folder():
    body = alert_rule_body(parse_alert_rule(API_RULE), folder_uid="different-folder")
    assert body["folderUID"] == "different-folder"


def test_alert_rule_body_keeps_unknown_future_fields():
    # Replaying the raw payload means a field this tool has never heard of
    # still round-trips.
    body = alert_rule_body(parse_alert_rule({**API_RULE, "someNewGrafanaField": {"x": 1}}))
    assert body["someNewGrafanaField"] == {"x": 1}


def test_recording_rule_omits_empty_evaluation_fields():
    # Grafana returns these as "" for recording rules, and rejects "" on write.
    recording = {
        **API_RULE,
        "record": {"metric": "my_metric", "from": "A"},
        "condition": "",
        "noDataState": "",
        "execErrState": "",
    }
    body = alert_rule_body(parse_alert_rule(recording))
    assert "condition" not in body
    assert "noDataState" not in body
    assert "execErrState" not in body
    assert body["record"] == {"metric": "my_metric", "from": "A"}


def test_alerting_rule_keeps_its_evaluation_fields():
    body = alert_rule_body(parse_alert_rule(API_RULE))
    assert body["condition"] == "B"
    assert body["noDataState"] == "NoData"
    assert body["execErrState"] == "Alerting"


def test_recording_rule_with_populated_evaluation_fields_keeps_them():
    # Only empties are dropped, so a rule that genuinely carries them is intact.
    recording = {**API_RULE, "record": {"metric": "m"}}
    body = alert_rule_body(parse_alert_rule(recording))
    assert body["condition"] == "B"


# ---------------------------------------------------------------------------
# contact points
# ---------------------------------------------------------------------------

PAGERDUTY = SourceContactPoint(
    uid="cp-1",
    name="critical-pagerduty",
    type="pagerduty",
    settings={"severity": "critical"},
    secure_field_names=("integrationKey",),
)


def test_contact_point_body_merges_supplied_secrets_inline():
    result = contact_point_body(PAGERDUTY, secrets={"integrationKey": "abc123"})
    assert result.body["settings"]["integrationKey"] == "abc123"
    assert result.body["settings"]["severity"] == "critical"
    assert result.secure_fields_supplied == ("integrationKey",)
    assert result.secure_fields_missing == ()


def test_contact_point_body_omits_rather_than_empties_an_unsupplied_secret():
    # Sending "" either 400s or wipes an existing value, so the key must go.
    result = contact_point_body(PAGERDUTY)
    assert "integrationKey" not in result.body["settings"]
    assert result.secure_fields_missing == ("integrationKey",)
    assert result.secure_fields_supplied == ()


def test_contact_point_body_treats_an_empty_string_secret_as_not_supplied():
    result = contact_point_body(PAGERDUTY, secrets={"integrationKey": ""})
    assert "integrationKey" not in result.body["settings"]
    assert result.secure_fields_missing == ("integrationKey",)


def test_contact_point_body_never_carries_the_redaction_sentinel():
    from grafana_migrator.source_dump import parse_contact_point

    cp = parse_contact_point(
        {
            "uid": "cp-2",
            "name": "slack",
            "type": "slack",
            "settings": {"recipient": "#ops", "url": "[REDACTED]"},
            "secureFields": {"url": True},
        }
    )
    result = contact_point_body(cp)
    assert "[REDACTED]" not in str(result.body)
    assert result.secure_fields_missing == ("url",)


def test_contact_point_body_includes_disable_resolve_message_only_when_set():
    assert "disableResolveMessage" not in contact_point_body(PAGERDUTY).body
    noisy = SourceContactPoint(uid="cp-3", name="x", type="webhook", settings={}, disable_resolve_message=True)
    assert contact_point_body(noisy).body["disableResolveMessage"] is True


def test_contact_point_body_does_not_mutate_the_source_settings():
    contact_point_body(PAGERDUTY, secrets={"integrationKey": "abc"})
    assert PAGERDUTY.settings == {"severity": "critical"}


# ---------------------------------------------------------------------------
# notification policy
# ---------------------------------------------------------------------------


def test_policy_body_strips_provenance_at_every_depth():
    policy = SourceNotificationPolicy(
        route={
            "receiver": "root",
            "provenance": "file",
            "routes": [
                {"receiver": "a", "provenance": "api"},
                {"receiver": "b", "routes": [{"receiver": "c", "provenance": "file"}]},
            ],
        }
    )
    body = notification_policy_body(policy)
    assert "provenance" not in str(body)
    assert body["routes"][1]["routes"][0]["receiver"] == "c"


def test_policy_body_leaves_matchers_in_whatever_form_grafana_returned():
    # The object_matchers rewrite is a grafana-operator workaround; doing it
    # here would corrupt a payload that already round-trips.
    policy = SourceNotificationPolicy(
        route={
            "receiver": "root",
            "routes": [
                {"receiver": "a", "matchers": ["severity=critical"]},
                {"receiver": "b", "object_matchers": [["team", "=", "core"]]},
            ],
        }
    )
    body = notification_policy_body(policy)
    assert body["routes"][0]["matchers"] == ["severity=critical"]
    assert body["routes"][1]["object_matchers"] == [["team", "=", "core"]]


def test_policy_body_survives_an_empty_tree():
    assert notification_policy_body(SourceNotificationPolicy(route={})) == {}
