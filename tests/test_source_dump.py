from pathlib import Path
from typing import Any

import pytest

from grafana_migrator.source_dump import (
    SourceDump,
    SourceDumpError,
    parse_alert_rule,
    parse_contact_point,
    read_source_dump,
    write_source_dump,
)

SEARCH_RESULTS = [
    {"uid": "folder-1", "title": "Team Alerts", "type": "dash-folder"},
    {
        "uid": "dash-1",
        "title": "CPU Overview",
        "type": "dash-db",
        "folderUid": "folder-1",
        "folderTitle": "Team Alerts",
    },
]
DASHBOARD_PAYLOAD = {"dashboard": {"uid": "dash-1", "title": "CPU Overview", "panels": []}, "meta": {"version": 3}}


def _sample_dump(**overrides) -> SourceDump:
    fields: dict[str, Any] = dict(
        search_results=SEARCH_RESULTS,
        dashboards_by_uid={"dash-1": DASHBOARD_PAYLOAD},
        alert_rules_raw=[{"uid": "rule-1"}],
        contact_points_raw=[{"uid": "cp-1"}],
        notification_policy_raw={"receiver": "empty"},
    )
    fields.update(overrides)
    return SourceDump(**fields)


def test_round_trip_preserves_every_field(tmp_path):
    write_source_dump(_sample_dump(), tmp_path)
    result = read_source_dump(tmp_path)
    assert result.search_results == SEARCH_RESULTS
    assert result.dashboards_by_uid == {"dash-1": DASHBOARD_PAYLOAD}
    assert result.alert_rules_raw == [{"uid": "rule-1"}]
    assert result.contact_points_raw == [{"uid": "cp-1"}]
    assert result.notification_policy_raw == {"receiver": "empty"}


def test_round_trip_distinguishes_not_fetched_from_empty(tmp_path):
    # alert_rules_raw=[] means "fetched, and there were none"; None means
    # "never fetched" (e.g. --skip-alerts at export time). These must not
    # collapse into the same on-disk representation.
    write_source_dump(
        _sample_dump(alert_rules_raw=None, contact_points_raw=None, notification_policy_raw=None), tmp_path
    )
    result = read_source_dump(tmp_path)
    assert result.alert_rules_raw is None
    assert result.contact_points_raw is None
    assert result.notification_policy_raw is None


def test_round_trip_empty_but_fetched_alert_rules(tmp_path):
    write_source_dump(_sample_dump(alert_rules_raw=[]), tmp_path)
    result = read_source_dump(tmp_path)
    assert result.alert_rules_raw == []


def test_read_source_dump_rejects_directory_missing_search_json(tmp_path):
    with pytest.raises(SourceDumpError):
        read_source_dump(tmp_path)


def test_read_source_dump_on_nonexistent_directory_raises():
    with pytest.raises(SourceDumpError):
        read_source_dump(Path("/no/such/directory/anywhere"))


def test_write_source_dump_writes_one_file_per_dashboard(tmp_path):
    write_source_dump(_sample_dump(), tmp_path)
    assert (tmp_path / "dashboards" / "dash-1.json").is_file()
    assert (tmp_path / "search.json").is_file()
    assert (tmp_path / "alert-rules.json").is_file()
    assert (tmp_path / "contact-points.json").is_file()
    assert (tmp_path / "notification-policy.json").is_file()
    assert (tmp_path / "meta.json").is_file()


# Shaped like a real GET /api/v1/provisioning/alert-rules entry (snake_case
# notification_settings/keep_firing_for, dashboard link only in annotations).
REAL_API_ALERT_RULE = {
    "id": 2,
    "uid": "UnschedulablePods_id",
    "orgID": 1,
    "folderUID": "bfu2bjcz3jugwd",
    "ruleGroup": "Cluster Autoscaler",
    "title": "Unschedulable pods",
    "condition": "C",
    "data": [{"refId": "A", "datasourceUid": "Prometheus", "model": {}}],
    "updated": "2024-01-01T00:00:00Z",
    "noDataState": "OK",
    "execErrState": "Error",
    "for": "20m",
    "keep_firing_for": "0s",
    "annotations": {
        "__dashboardUid__": "cluster_autoscaler_overview",
        "__panelId__": "12",
        "summary": "unschedulable pods",
    },
    "labels": {"severity": "critical"},
    "provenance": "file",
    "isPaused": False,
    "notification_settings": None,
    "record": None,
}


def testparse_alert_rule_reads_snake_case_fields_not_camel_case():
    rule = parse_alert_rule(REAL_API_ALERT_RULE)
    assert rule.keep_firing_for == "0s"
    assert rule.notification_settings is None


def testparse_alert_rule_extracts_dashboard_and_panel_from_annotations():
    rule = parse_alert_rule(REAL_API_ALERT_RULE)
    assert rule.dashboard_uid == "cluster_autoscaler_overview"
    assert rule.panel_id == 12
    # the raw annotation keys are still preserved for the CR's own annotations
    assert rule.annotations["__dashboardUid__"] == "cluster_autoscaler_overview"


def testparse_alert_rule_without_dashboard_annotations_leaves_them_none():
    raw = dict(REAL_API_ALERT_RULE)
    raw["annotations"] = {"summary": "no dashboard link here"}
    rule = parse_alert_rule(raw)
    assert rule.dashboard_uid is None
    assert rule.panel_id is None


def testparse_contact_point_reads_secure_field_names_from_secure_fields_map():
    raw = {
        "uid": "slack-uid",
        "name": "Platform Slack",
        "type": "slack",
        "settings": {"recipient": "#platform-alerts"},
        "secureFields": {"url": True},
        "disableResolveMessage": False,
    }
    cp = parse_contact_point(raw)
    assert cp.secure_field_names == ("url",)
    assert cp.settings == {"recipient": "#platform-alerts"}


def testparse_contact_point_with_no_secure_fields():
    raw = {"uid": "email-uid", "name": "Platform Email", "type": "email", "settings": {"addresses": "a@b.com"}}
    cp = parse_contact_point(raw)
    assert cp.secure_field_names == ()


def testparse_contact_point_detects_inline_redacted_sentinel():
    # Some Grafana versions redact secure fields in-place with "[REDACTED]".
    raw = {
        "uid": "slack-uid",
        "name": "Platform Slack",
        "type": "slack",
        "settings": {"recipient": "#platform-alerts", "url": "[REDACTED]"},
        "disableResolveMessage": False,
    }
    cp = parse_contact_point(raw)
    assert cp.secure_field_names == ("url",)
    # the sentinel must never end up in settings -- it isn't a real value
    assert cp.settings == {"recipient": "#platform-alerts"}
    assert "url" not in cp.settings


def testparse_contact_point_merges_secure_fields_map_and_inline_sentinel():
    raw = {
        "uid": "webhook-uid",
        "name": "Platform Webhook",
        "type": "webhook",
        "settings": {"url": "https://example.com/hook", "password": "[REDACTED]"},
        "secureFields": {"authorization_credentials": True},
    }
    cp = parse_contact_point(raw)
    assert cp.secure_field_names == ("authorization_credentials", "password")
    assert cp.settings == {"url": "https://example.com/hook"}
