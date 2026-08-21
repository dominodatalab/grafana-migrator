from typing import Any
from pathlib import Path

from grafana_migrator import cli
from grafana_migrator.cli import _existing_manifest_subdirs, _manifest_subdirs, _parse_alert_rule, _parse_contact_point

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


def test_parse_alert_rule_reads_snake_case_fields_not_camel_case():
    rule = _parse_alert_rule(REAL_API_ALERT_RULE)
    assert rule.keep_firing_for == "0s"
    assert rule.notification_settings is None


def test_parse_alert_rule_extracts_dashboard_and_panel_from_annotations():
    rule = _parse_alert_rule(REAL_API_ALERT_RULE)
    assert rule.dashboard_uid == "cluster_autoscaler_overview"
    assert rule.panel_id == 12
    # the raw annotation keys are still preserved for the CR's own annotations
    assert rule.annotations["__dashboardUid__"] == "cluster_autoscaler_overview"


def test_parse_alert_rule_without_dashboard_annotations_leaves_them_none():
    raw = dict(REAL_API_ALERT_RULE)
    raw["annotations"] = {"summary": "no dashboard link here"}
    rule = _parse_alert_rule(raw)
    assert rule.dashboard_uid is None
    assert rule.panel_id is None


def test_parse_contact_point_reads_secure_field_names_from_secure_fields_map():
    raw = {
        "uid": "slack-uid",
        "name": "Platform Slack",
        "type": "slack",
        "settings": {"recipient": "#platform-alerts"},
        "secureFields": {"url": True},
        "disableResolveMessage": False,
    }
    cp = _parse_contact_point(raw)
    assert cp.secure_field_names == ("url",)
    assert cp.settings == {"recipient": "#platform-alerts"}


def test_parse_contact_point_with_no_secure_fields():
    raw = {"uid": "email-uid", "name": "Platform Email", "type": "email", "settings": {"addresses": "a@b.com"}}
    cp = _parse_contact_point(raw)
    assert cp.secure_field_names == ()


def test_parse_contact_point_detects_inline_redacted_sentinel():
    # Some Grafana versions redact secure fields in-place with "[REDACTED]".
    raw = {
        "uid": "slack-uid",
        "name": "Platform Slack",
        "type": "slack",
        "settings": {"recipient": "#platform-alerts", "url": "[REDACTED]"},
        "disableResolveMessage": False,
    }
    cp = _parse_contact_point(raw)
    assert cp.secure_field_names == ("url",)
    # the sentinel must never end up in settings -- it isn't a real value
    assert cp.settings == {"recipient": "#platform-alerts"}
    assert "url" not in cp.settings


def test_parse_contact_point_merges_secure_fields_map_and_inline_sentinel():
    raw = {
        "uid": "webhook-uid",
        "name": "Platform Webhook",
        "type": "webhook",
        "settings": {"url": "https://example.com/hook", "password": "[REDACTED]"},
        "secureFields": {"authorization_credentials": True},
    }
    cp = _parse_contact_point(raw)
    assert cp.secure_field_names == ("authorization_credentials", "password")
    assert cp.settings == {"url": "https://example.com/hook"}


def test_manifest_subdirs_excludes_report_json():
    # report.json lives directly under --output-dir, not in a subdirectory --
    # it must never end up in the set of dirs handed to `kubectl apply`.
    manifests: list[tuple[str, dict[str, Any]]] = [
        ("dashboards/migrated-a.yaml", {}),
        ("folders/migrated-b.yaml", {}),
        ("alert-rules/migrated-c.yaml", {}),
    ]
    assert _manifest_subdirs(manifests) == ["alert-rules", "dashboards", "folders"]


def test_manifest_subdirs_empty_when_nothing_written():
    assert _manifest_subdirs([]) == []


def test_manifest_subdirs_dedupes_multiple_files_per_directory():
    manifests: list[tuple[str, dict[str, Any]]] = [
        ("contact-points/migrated-a.yaml", {}),
        ("contact-points/migrated-a-secrets.yaml", {}),
    ]
    assert _manifest_subdirs(manifests) == ["contact-points"]


def test_existing_manifest_subdirs_finds_dirs_with_yaml_files(tmp_path):
    (tmp_path / "dashboards").mkdir()
    (tmp_path / "dashboards" / "migrated-a.yaml").write_text("kind: GrafanaDashboard\n")
    (tmp_path / "folders").mkdir()
    (tmp_path / "folders" / "migrated-b.yaml").write_text("kind: GrafanaFolder\n")
    (tmp_path / "report.json").write_text("{}")
    assert _existing_manifest_subdirs(tmp_path) == ["dashboards", "folders"]


def test_existing_manifest_subdirs_ignores_empty_directories(tmp_path):
    (tmp_path / "empty-leftover-dir").mkdir()
    (tmp_path / "dashboards").mkdir()
    (tmp_path / "dashboards" / "migrated-a.yaml").write_text("kind: GrafanaDashboard\n")
    assert _existing_manifest_subdirs(tmp_path) == ["dashboards"]


def test_existing_manifest_subdirs_on_nonexistent_dir_returns_empty():
    assert _existing_manifest_subdirs(Path("/no/such/directory/anywhere")) == []


def _recorder(calls, label=None):
    """Stand-in for a run_* entry point: record the argv it saw, exit 0."""

    def run(argv):
        calls.append((label, argv) if label else argv)
        return 0

    return run


def test_run_dispatches_apply_subcommand(monkeypatch):
    calls: list[Any] = []
    monkeypatch.setattr(cli, "run_apply", _recorder(calls, "apply"))
    monkeypatch.setattr(cli, "run_export", _recorder(calls, "export"))
    monkeypatch.setattr(cli, "run_import", _recorder(calls, "import"))
    cli.run(["apply", "./some-import-dir", "--kube-context", "my-ctx"])
    assert calls == [("apply", ["./some-import-dir", "--kube-context", "my-ctx"])]


def test_run_dispatches_export_subcommand(monkeypatch):
    calls: list[Any] = []
    monkeypatch.setattr(cli, "run_export", _recorder(calls))
    cli.run(["export", "--source-url", "http://localhost:18090"])
    assert calls == [["--source-url", "http://localhost:18090"]]


def test_run_dispatches_import_subcommand(monkeypatch):
    calls: list[Any] = []
    monkeypatch.setattr(cli, "run_import", _recorder(calls))
    cli.run(["import", "./some-snapshot-dir", "--namespace", "monitoring"])
    assert calls == [["./some-snapshot-dir", "--namespace", "monitoring"]]


def test_run_requires_a_known_subcommand(capsys):
    assert cli.run([]) == 2
    assert cli.run(["--source-url", "http://localhost:18090"]) == 2
    assert "export" in capsys.readouterr().err
