from pathlib import Path
from typing import Any

from grafana_migrator import cli
from grafana_migrator.cli import _existing_manifest_subdirs, _manifest_subdirs


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
