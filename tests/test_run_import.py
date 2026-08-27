"""End-to-end coverage of `grafana-migrator import`: feed it a source snapshot
written by `write_source_dump` and a target cluster with nothing existing on
it (kubectl reads mocked out, since import's whole point is to work without
live cluster access to the *source* -- but it still needs a target to dedup
against, mocked here so the test doesn't need a real cluster).
"""

from __future__ import annotations

import json

import yaml
from fake_session import FakeResponse, FakeSession

from grafana_migrator import cli, grafana_client, k8s_inventory
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


def _mock_target(monkeypatch, inventory=None):
    """Stub the single kubectl entry point, keyed by CRD name.

    Patching here rather than at the list_existing_* helpers keeps these tests
    pinned to a seam that survives refactoring, and exercises the CR-parsing
    those helpers do instead of bypassing it.
    """
    items = inventory or {}
    monkeypatch.setattr(
        k8s_inventory,
        "_kubectl_get_json",
        lambda resource, namespace, context: {"items": items.get(resource, [])},
    )


def _mock_empty_target(monkeypatch):
    _mock_target(monkeypatch)


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
    export_dir = _write_dump(tmp_path)
    _mock_target(
        monkeypatch,
        {
            "grafanadashboards.grafana.integreatly.org": [
                {
                    "metadata": {"name": "migrated-dash-1"},
                    "spec": {"json": json.dumps({"uid": "dash-1", "title": "CPU Overview"})},
                }
            ]
        },
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


# ---------------------------------------------------------------------------
# --target api, end to end through the CLI
# ---------------------------------------------------------------------------

_API_ROUTES = {
    ("GET", "/api/search"): FakeResponse(200, []),
    ("GET", "/api/v1/provisioning/alert-rules"): FakeResponse(200, []),
    ("GET", "/api/v1/provisioning/contact-points"): FakeResponse(200, []),
    ("GET", "/api/v1/provisioning/policies"): FakeResponse(200, {"receiver": "empty"}),
    ("POST", "/api/folders"): FakeResponse(200, {"uid": "folder-1"}),
    ("POST", "/api/dashboards/db"): FakeResponse(200, {"uid": "dash-1"}),
    ("POST", "/api/v1/provisioning/alert-rules"): FakeResponse(200, {}),
    ("POST", "/api/v1/provisioning/contact-points"): FakeResponse(200, {}),
    ("PUT", "/api/v1/provisioning/policies"): FakeResponse(200, {}),
}


def _mock_api_target(monkeypatch, routes=None):
    """Capture the session the CLI builds, so calls can be asserted on.

    Wraps grafana_client.build_client rather than cli.build_client: patching
    twice in one test would otherwise wrap the previous wrapper and keep
    injecting the first session.
    """
    session = FakeSession(dict(routes or _API_ROUTES))

    def build(**kw):
        kw["session"] = session
        return grafana_client.build_client(**kw)

    monkeypatch.setattr(cli, "build_client", build)
    return session


def _api_argv(export_dir, tmp_path, *extra):
    return [
        str(export_dir),
        "--target",
        "api",
        "--dest-url",
        "http://graf.test",
        "--dest-token",
        "glsa_tok",
        "--output-dir",
        str(tmp_path / "api-out"),
        "--report-format",
        "json",
        *extra,
    ]


def test_api_import_pushes_everything_and_writes_only_a_report(tmp_path, monkeypatch, capsys):
    export_dir = _write_dump(tmp_path)
    session = _mock_api_target(monkeypatch)

    rc = cli.run_import(_api_argv(export_dir, tmp_path))
    assert rc == 0

    writes = [(m, p) for m, p, _, _ in session.calls if m != "GET"]
    assert ("POST", "/api/folders") in writes
    assert ("POST", "/api/dashboards/db") in writes

    out_dir = tmp_path / "api-out"
    # api mode writes the report as its resume record, and no manifests
    assert (out_dir / "report.json").is_file()
    assert not (out_dir / "dashboards").exists()

    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["backend"] == "api"
    assert report["summary"]["dashboards_migrated"] >= 1


def test_api_dry_run_issues_no_writes_and_creates_no_output_dir(tmp_path, monkeypatch, capsys):
    export_dir = _write_dump(tmp_path)
    session = _mock_api_target(monkeypatch)

    rc = cli.run_import(_api_argv(export_dir, tmp_path, "--dry-run"))
    assert rc == 0
    assert [m for m, _, _, _ in session.calls if m != "GET"] == []
    assert not (tmp_path / "api-out").exists()
    assert json.loads(capsys.readouterr().out)["summary"]["backend"] == "api"


def test_api_import_is_provisioned_by_default_and_editable_on_request(tmp_path, monkeypatch):
    export_dir = _write_dump(tmp_path)

    session = _mock_api_target(monkeypatch)
    cli.run_import(_api_argv(export_dir, tmp_path))
    assert "X-Disable-Provenance" not in session.headers

    session = _mock_api_target(monkeypatch)
    cli.run_import(_api_argv(export_dir, tmp_path, "--editable"))
    assert session.headers["X-Disable-Provenance"] == "true"


def test_api_import_exits_1_when_an_object_fails(tmp_path, monkeypatch, capsys):
    export_dir = _write_dump(tmp_path)
    routes = dict(_API_ROUTES)
    routes[("POST", "/api/dashboards/db")] = FakeResponse(400, {"message": "bad panel"})
    _mock_api_target(monkeypatch, routes)

    rc = cli.run_import(_api_argv(export_dir, tmp_path))
    assert rc == 1
    report = json.loads(capsys.readouterr().out)
    assert report["summary"]["failures"] >= 1
    # the report still lands, which is the point of writing it on failure
    assert (tmp_path / "api-out" / "report.json").is_file()


def test_api_import_reports_a_bad_target_url_cleanly(tmp_path, monkeypatch, capsys):
    export_dir = _write_dump(tmp_path)
    _mock_api_target(monkeypatch, {("GET", "/api/search"): FakeResponse(401, {"message": "no"})})

    rc = cli.run_import(_api_argv(export_dir, tmp_path))
    assert rc == 1
    assert "--dest-token" in capsys.readouterr().err


def test_operator_and_api_agree_on_what_is_new(tmp_path, monkeypatch, capsys):
    """Same snapshot, both backends, empty target: identical migrate counts."""
    export_dir = _write_dump(tmp_path)

    _mock_empty_target(monkeypatch)
    assert (
        cli.run_import(
            [
                str(export_dir),
                "--namespace",
                "monitoring",
                "--instance-selector",
                "dashboards=my-grafana",
                "--output-dir",
                str(tmp_path / "op-out"),
                "--report-format",
                "json",
            ]
        )
        == 0
    )
    operator = json.loads(capsys.readouterr().out)["summary"]

    _mock_api_target(monkeypatch)
    assert cli.run_import(_api_argv(export_dir, tmp_path)) == 0
    api = json.loads(capsys.readouterr().out)["summary"]

    for key in (
        "dashboards_discovered",
        "dashboards_migrated",
        "folders_created",
        "alert_rules_migrated",
        "contact_points_migrated",
    ):
        assert operator[key] == api[key], key
