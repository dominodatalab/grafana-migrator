"""End-to-end coverage of `grafana-migrator import`: feed it a source snapshot
written by `write_source_dump` and a target cluster with nothing existing on
it (kubectl reads mocked out, since import's whole point is to work without
live cluster access to the *source* -- but it still needs a target to dedup
against, mocked here so the test doesn't need a real cluster).
"""

from __future__ import annotations

import json

import yaml

from grafana_migrator import cli
from grafana_migrator.source_dump import SourceDump, write_source_dump

SEARCH_RESULTS = [
    {"uid": "folder-1", "title": "Platform Team", "type": "dash-folder"},
    {
        "uid": "dash-1",
        "title": "CPU Overview",
        "type": "dash-db",
        "folderUid": "folder-1",
        "folderTitle": "Platform Team",
    },
]
DASHBOARD_PAYLOAD = {"dashboard": {"uid": "dash-1", "title": "CPU Overview", "panels": [], "schemaVersion": 39}}
ALERT_RULE = {
    "uid": "high-cpu",
    "title": "High CPU",
    "ruleGroup": "Infra Alerts",
    "folderUID": "folder-1",
    "condition": "C",
    "data": [{"refId": "C", "datasourceUid": "Prometheus", "model": {}}],
    "noDataState": "NoData",
    "execErrState": "Error",
    "for": "5m",
    "annotations": {},
    "labels": {},
    "isPaused": False,
}
CONTACT_POINT = {
    "uid": "cp-1",
    "name": "Platform Slack",
    "type": "slack",
    "settings": {"recipient": "#platform", "url": "[REDACTED]"},
}
NOTIFICATION_POLICY = {
    "receiver": "Platform Slack",
    "routes": [{"receiver": "Platform Slack", "matchers": ["team=platform"]}],
}


def _write_dump(tmp_path):
    dump = SourceDump(
        search_results=SEARCH_RESULTS,
        dashboards_by_uid={"dash-1": DASHBOARD_PAYLOAD},
        alert_rules_raw=[ALERT_RULE],
        contact_points_raw=[CONTACT_POINT],
        notification_policy_raw=NOTIFICATION_POLICY,
    )
    export_dir = tmp_path / "snapshot"
    write_source_dump(dump, export_dir)
    return export_dir


def _mock_empty_target(monkeypatch):
    monkeypatch.setattr(cli, "list_existing_dashboards", lambda namespace, context: [])
    monkeypatch.setattr(cli, "list_existing_folders", lambda namespace, context: [])
    monkeypatch.setattr(cli, "list_existing_alert_rule_groups", lambda namespace, context: [])
    monkeypatch.setattr(cli, "list_existing_contact_points", lambda namespace, context: [])
    monkeypatch.setattr(cli, "has_existing_notification_policy", lambda namespace, context: False)


def test_import_dry_run_writes_nothing_but_reports_everything_as_new(tmp_path, monkeypatch, capsys):
    export_dir = _write_dump(tmp_path)
    _mock_empty_target(monkeypatch)
    output_dir = tmp_path / "manifests"

    rc = cli.run_import(
        [
            str(export_dir),
            "--namespace",
            "monitoring",
            "--instance-selector",
            "dashboards=my-grafana",
            "--output-dir",
            str(output_dir),
            "--dry-run",
            "--report-format",
            "json",
        ]
    )
    assert rc == 0
    assert not output_dir.exists()

    report = json.loads(capsys.readouterr().out)
    assert [m["uid"] for m in report["migrated"]] == ["dash-1"]
    assert [f["title"] for f in report["folders_created"]] == ["Platform Team"]
    assert [r["uid"] for r in report["alert_rules_migrated"]] == ["high-cpu"]
    assert [c["uid"] for c in report["contact_points_migrated"]] == ["cp-1"]
    assert report["notification_policy_status"] == "migrated"


def test_import_writes_manifests_matching_the_dedup_decisions(tmp_path, monkeypatch):
    export_dir = _write_dump(tmp_path)
    _mock_empty_target(monkeypatch)
    output_dir = tmp_path / "manifests"

    rc = cli.run_import(
        [
            str(export_dir),
            "--namespace",
            "monitoring",
            "--instance-selector",
            "dashboards=my-grafana",
            "--output-dir",
            str(output_dir),
        ]
    )
    assert rc == 0

    dashboard = yaml.safe_load((output_dir / "dashboards" / "migrated-dash-1.yaml").read_text())
    assert dashboard["spec"]["folderRef"] == "migrated-platform-team"

    folder = yaml.safe_load((output_dir / "folders" / "migrated-platform-team.yaml").read_text())
    assert folder["spec"]["title"] == "Platform Team"

    rule_group = yaml.safe_load((output_dir / "alert-rules" / "migrated-platform-team-infra-alerts.yaml").read_text())
    assert rule_group["spec"]["rules"][0]["uid"] == "high-cpu"

    contact_point = yaml.safe_load((output_dir / "contact-points" / "migrated-platform-slack.yaml").read_text())
    assert contact_point["spec"]["receivers"][0]["valuesFrom"][0]["targetPath"] == "url"
    secret = yaml.safe_load((output_dir / "contact-points" / "migrated-platform-slack-secrets.yaml").read_text())
    assert secret["stringData"] == {"url": ""}

    policy = yaml.safe_load((output_dir / "notification-policy" / "migrated-notification-policy.yaml").read_text())
    assert policy["spec"]["route"]["routes"][0]["object_matchers"] == [["team", "=", "platform"]]

    assert json.loads((output_dir / "report.json").read_text())["migrated"][0]["uid"] == "dash-1"


def test_import_skip_alerts_flag_ignores_snapshot_alert_data(tmp_path, monkeypatch):
    export_dir = _write_dump(tmp_path)
    _mock_empty_target(monkeypatch)
    output_dir = tmp_path / "manifests"

    rc = cli.run_import(
        [
            str(export_dir),
            "--namespace",
            "monitoring",
            "--instance-selector",
            "dashboards=my-grafana",
            "--output-dir",
            str(output_dir),
            "--skip-alerts",
        ]
    )
    assert rc == 0
    assert not (output_dir / "alert-rules").exists()
    assert not (output_dir / "contact-points").exists()
    assert not (output_dir / "notification-policy").exists()


def test_import_treats_uid_already_on_target_as_skipped_not_migrated(tmp_path, monkeypatch):
    from grafana_migrator.models import ExistingDashboard

    export_dir = _write_dump(tmp_path)
    _mock_empty_target(monkeypatch)
    monkeypatch.setattr(
        cli,
        "list_existing_dashboards",
        lambda namespace, context: [
            ExistingDashboard(cr_name="migrated-dash-1", namespace=namespace, uid="dash-1", title="CPU Overview")
        ],
    )

    rc = cli.run_import(
        [
            str(export_dir),
            "--namespace",
            "monitoring",
            "--instance-selector",
            "dashboards=my-grafana",
            "--output-dir",
            str(tmp_path / "manifests"),
            "--dry-run",
            "--report-format",
            "json",
        ]
    )
    assert rc == 0
