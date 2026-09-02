import argparse
from pathlib import Path
from typing import Any

from grafana_migrator import cli
from grafana_migrator.cli import _existing_manifest_subdirs, _manifest_subdirs, _validate_import_args


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


# ---------------------------------------------------------------------------
# --target validation matrix
# ---------------------------------------------------------------------------


def _import_args(**kw):
    base = dict(
        export_dir="./snap",
        target="operator",
        namespace=None,
        kube_context=None,
        instance_selector=None,
        dest_url=None,
        dest_path_segment=None,
        dest_token=None,
        dest_user=None,
        dest_password=None,
        editable=False,
        stop_on_first_error=False,
        output_dir="./out",
        include_title_duplicates=False,
        skip_alerts=False,
        skip_notification_policy=False,
        dry_run=False,
        apply=False,
        secrets_file=None,
        write_secrets_skeleton=None,
        report_format="text",
        verbose=False,
    )
    base.update(kw)
    return argparse.Namespace(**base)


def test_target_defaults_to_operator():
    args = cli.build_import_parser().parse_args(["./snap", "--namespace", "ns", "--instance-selector", "a=b"])
    assert args.target == "operator"


def test_operator_target_requires_namespace_and_selector():
    assert "namespace" in (_validate_import_args(_import_args()) or "")
    assert "instance-selector" in (_validate_import_args(_import_args(namespace="ns")) or "")
    assert _validate_import_args(_import_args(namespace="ns", instance_selector={"a": "b"})) is None


def test_api_target_requires_a_dest_url():
    problem = _validate_import_args(_import_args(target="api"))
    assert "--dest-url" in (problem or "")


def test_api_target_requires_credentials():
    problem = _validate_import_args(_import_args(target="api", dest_url="http://g"))
    assert "credentials" in (problem or "")


def test_api_target_accepts_a_token_or_basic_auth():
    assert _validate_import_args(_import_args(target="api", dest_url="http://g", dest_token="t")) is None
    assert (
        _validate_import_args(_import_args(target="api", dest_url="http://g", dest_user="u", dest_password="p")) is None
    )


def test_api_target_rejects_half_given_basic_auth():
    problem = _validate_import_args(_import_args(target="api", dest_url="http://g", dest_user="u"))
    assert "credentials" in (problem or "")


def test_api_target_rejects_apply():
    problem = _validate_import_args(_import_args(target="api", dest_url="http://g", dest_token="t", apply=True))
    assert "--target operator" in (problem or "")


def test_api_target_rejects_cluster_flags_rather_than_ignoring_them():
    # Silently ignoring a selector would read as "my CRs got these labels".
    problem = _validate_import_args(_import_args(target="api", dest_url="http://g", dest_token="t", namespace="ns"))
    assert "only apply to --target operator" in (problem or "")


def test_apply_and_dry_run_conflict_regardless_of_target():
    problem = _validate_import_args(
        _import_args(namespace="ns", instance_selector={"a": "b"}, apply=True, dry_run=True)
    )
    assert "--apply cannot be combined with --dry-run" in (problem or "")


def test_validation_problems_exit_2(capsys):
    rc = cli.run_import(["./snap", "--target", "api"])
    assert rc == 2
    assert "--dest-url" in capsys.readouterr().err
