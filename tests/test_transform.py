import json

from grafana_migrator.models import SourceAlertRule, SourceContactPoint, SourceNotificationPolicy
from grafana_migrator.transform import (
    alert_rule_group_to_manifest,
    contact_point_to_manifest,
    dashboard_json_to_manifest,
    folder_title_to_manifest,
    is_default_contact_point,
    is_default_notification_policy,
    notification_policy_to_manifest,
)

SAMPLE_DASHBOARD = {
    "id": 42,
    "uid": "antvsq",
    "version": 3,
    "title": "Sample Persistence Test Dashboard",
    "panels": [],
}


def test_dashboard_manifest_strips_id_and_version_but_keeps_uid():
    manifest = dashboard_json_to_manifest(
        SAMPLE_DASHBOARD,
        name="migrated-antvsq",
        namespace="monitoring",
        instance_selector={"dashboards": "my-grafana"},
        source_uid="antvsq",
        source_title="Sample Persistence Test Dashboard",
        folder="General",
    )
    embedded = json.loads(manifest["spec"]["json"])
    assert "id" not in embedded
    assert "version" not in embedded
    assert embedded["uid"] == "antvsq"


def test_dashboard_manifest_uses_folder_when_no_folder_ref():
    manifest = dashboard_json_to_manifest(
        SAMPLE_DASHBOARD,
        name="migrated-antvsq",
        namespace="monitoring",
        instance_selector={"dashboards": "my-grafana"},
        source_uid="antvsq",
        source_title="t",
        folder="Sample Persistence Test",
    )
    assert manifest["spec"]["folder"] == "Sample Persistence Test"
    assert "folderRef" not in manifest["spec"]


def test_dashboard_manifest_uses_folder_ref_when_provided():
    manifest = dashboard_json_to_manifest(
        SAMPLE_DASHBOARD,
        name="migrated-afzndm",
        namespace="monitoring",
        instance_selector={"dashboards": "my-grafana"},
        source_uid="afzndm",
        source_title="t",
        folder_ref="team-managed",
    )
    assert manifest["spec"]["folderRef"] == "team-managed"
    assert "folder" not in manifest["spec"]


def test_dashboard_manifest_shape():
    manifest = dashboard_json_to_manifest(
        SAMPLE_DASHBOARD,
        name="migrated-antvsq",
        namespace="monitoring",
        instance_selector={"dashboards": "my-grafana"},
        source_uid="antvsq",
        source_title="Sample Persistence Test Dashboard",
        folder="General",
    )
    assert manifest["apiVersion"] == "grafana.integreatly.org/v1beta1"
    assert manifest["kind"] == "GrafanaDashboard"
    assert manifest["metadata"]["name"] == "migrated-antvsq"
    assert manifest["metadata"]["annotations"]["grafana-migrator/source-uid"] == "antvsq"
    assert manifest["spec"]["instanceSelector"]["matchLabels"] == {"dashboards": "my-grafana"}


def test_folder_manifest_shape():
    manifest = folder_title_to_manifest(
        "Sample Persistence Test",
        name="migrated-sample-persistence-test",
        namespace="monitoring",
        instance_selector={"dashboards": "my-grafana"},
        source_uid="afu2hgzwtw5c0c",
    )
    assert manifest["kind"] == "GrafanaFolder"
    assert manifest["spec"]["title"] == "Sample Persistence Test"
    assert manifest["metadata"]["annotations"]["grafana-migrator/source-uid"] == "afu2hgzwtw5c0c"


SAMPLE_RULE = SourceAlertRule(
    uid="PVUsageCritical_id",
    title="PV Usage Critical",
    rule_group="Platform PV Usage",
    folder_uid="bfu2bjcz3jugwd",
    condition="C",
    data=[{"refId": "A", "datasourceUid": "Prometheus", "model": {}}],
    no_data_state="OK",
    exec_err_state="Error",
    for_="15m0s",
    annotations={"summary": "PV usage critical"},
    labels={"severity": "critical"},
)


def test_alert_rule_group_manifest_uses_folder_ref_when_provided():
    manifest = alert_rule_group_to_manifest(
        [SAMPLE_RULE],
        name="migrated-team-managed-platform-pv-usage",
        namespace="monitoring",
        instance_selector={"alerts": "my-grafana"},
        rule_group="Platform PV Usage",
        folder_ref="team-managed",
    )
    assert manifest["kind"] == "GrafanaAlertRuleGroup"
    assert manifest["spec"]["folderRef"] == "team-managed"
    assert "folderUID" not in manifest["spec"]
    assert manifest["spec"]["rules"][0]["uid"] == "PVUsageCritical_id"
    assert manifest["spec"]["rules"][0]["for"] == "15m0s"
    assert manifest["spec"]["rules"][0]["labels"] == {"severity": "critical"}


def test_alert_rule_group_manifest_uses_folder_uid_when_no_ref():
    manifest = alert_rule_group_to_manifest(
        [SAMPLE_RULE],
        name="migrated-bfu2bjcz3jugwd-platform-pv-usage",
        namespace="monitoring",
        instance_selector={"alerts": "my-grafana"},
        rule_group="Platform PV Usage",
        folder_uid="bfu2bjcz3jugwd",
    )
    assert manifest["spec"]["folderUID"] == "bfu2bjcz3jugwd"
    assert "folderRef" not in manifest["spec"]


def test_alert_rule_group_manifest_groups_multiple_rules():
    other = SourceAlertRule(
        uid="PVInodesUsageCritical_id",
        title="PV Inodes Usage Critical",
        rule_group="Platform PV Usage",
        folder_uid="bfu2bjcz3jugwd",
        condition="C",
        data=[],
        no_data_state="OK",
        exec_err_state="Error",
        for_="15m0s",
    )
    manifest = alert_rule_group_to_manifest(
        [SAMPLE_RULE, other],
        name="migrated-team-managed-platform-pv-usage",
        namespace="monitoring",
        instance_selector={"alerts": "my-grafana"},
        rule_group="Platform PV Usage",
        folder_ref="team-managed",
    )
    assert len(manifest["spec"]["rules"]) == 2
    assert manifest["spec"]["name"] == "Platform PV Usage"


DEFAULT_CONTACT_POINT = SourceContactPoint(uid="default-uid", name="empty", type="empty", settings={})
SLACK_CONTACT_POINT = SourceContactPoint(
    uid="slack-uid",
    name="Platform Slack",
    type="slack",
    settings={"recipient": "#platform-alerts"},
    secure_field_names=("url",),
)


def test_is_default_contact_point_detects_grafanas_builtin_receiver():
    assert is_default_contact_point(DEFAULT_CONTACT_POINT) is True
    assert is_default_contact_point(SLACK_CONTACT_POINT) is False


def test_contact_point_manifest_points_secure_fields_at_a_secret():
    manifest = contact_point_to_manifest(
        SLACK_CONTACT_POINT,
        name="migrated-platform-slack",
        namespace="monitoring",
        instance_selector={"alerts": "my-grafana"},
        secret_name="migrated-platform-slack-secrets",
    )
    receiver = manifest["spec"]["receivers"][0]
    assert receiver["settings"] == {"recipient": "#platform-alerts"}
    assert receiver["valuesFrom"] == [
        {
            "targetPath": "url",
            "valueFrom": {"secretKeyRef": {"name": "migrated-platform-slack-secrets", "key": "url"}},
        }
    ]


def test_contact_point_manifest_omits_values_from_when_no_secure_fields():
    cp = SourceContactPoint(uid="email-uid", name="Platform Email", type="email", settings={"addresses": "a@b.com"})
    manifest = contact_point_to_manifest(
        cp,
        name="migrated-platform-email",
        namespace="monitoring",
        instance_selector={"alerts": "my-grafana"},
        secret_name="migrated-platform-email-secrets",
    )
    assert "valuesFrom" not in manifest["spec"]["receivers"][0]


DEFAULT_POLICY = SourceNotificationPolicy(route={"receiver": "empty", "group_by": ["grafana_folder", "alertname"]})
CUSTOM_POLICY = SourceNotificationPolicy(
    route={
        "receiver": "empty",
        "group_by": ["grafana_folder", "alertname"],
        "routes": [{"receiver": "Platform Slack", "matchers": ["severity=critical"]}],
    }
)


def test_is_default_notification_policy_detects_factory_default():
    assert is_default_notification_policy(DEFAULT_POLICY) is True
    assert is_default_notification_policy(CUSTOM_POLICY) is False


def test_notification_policy_manifest_strips_provenance():
    policy = SourceNotificationPolicy(route={"receiver": "Platform Slack", "provenance": "api"})
    manifest = notification_policy_to_manifest(
        policy,
        name="migrated-notification-policy",
        namespace="monitoring",
        instance_selector={"alerts": "my-grafana"},
    )
    assert manifest["kind"] == "GrafanaNotificationPolicy"
    assert "provenance" not in manifest["spec"]["route"]
    assert manifest["spec"]["route"]["receiver"] == "Platform Slack"


def test_notification_policy_manifest_converts_string_matchers_to_object_matcher_triples():
    # grafana-operator 5.24.0 fails to translate structured `matchers` back
    # into a request Grafana's policy API accepts; object_matchers works.
    manifest = notification_policy_to_manifest(
        CUSTOM_POLICY,
        name="migrated-notification-policy",
        namespace="monitoring",
        instance_selector={"alerts": "my-grafana"},
    )
    nested_route = manifest["spec"]["route"]["routes"][0]
    assert "matchers" not in nested_route
    assert nested_route["object_matchers"] == [["severity", "=", "critical"]]


def test_notification_policy_manifest_handles_quoted_and_negated_matchers():
    policy = SourceNotificationPolicy(
        route={
            "receiver": "empty",
            "routes": [
                {
                    "receiver": "Platform Slack",
                    "matchers": ['team="migrator-test"', "severity!=info", 'env=~"prod.*"', "region!~west.*"],
                }
            ],
        }
    )
    manifest = notification_policy_to_manifest(
        policy, name="x", namespace="ns", instance_selector={"alerts": "my-grafana"}
    )
    assert manifest["spec"]["route"]["routes"][0]["object_matchers"] == [
        ["team", "=", "migrator-test"],
        ["severity", "!=", "info"],
        ["env", "=~", "prod.*"],
        ["region", "!~", "west.*"],
    ]


def test_notification_policy_manifest_converts_matchers_recursively_in_nested_routes():
    policy = SourceNotificationPolicy(
        route={
            "receiver": "empty",
            "routes": [
                {
                    "receiver": "Team A",
                    "matchers": ["team=a"],
                    "routes": [{"receiver": "Team A Critical", "matchers": ["severity=critical"]}],
                }
            ],
        }
    )
    manifest = notification_policy_to_manifest(
        policy, name="x", namespace="ns", instance_selector={"alerts": "my-grafana"}
    )
    grandchild = manifest["spec"]["route"]["routes"][0]["routes"][0]
    assert grandchild["object_matchers"] == [["severity", "=", "critical"]]
