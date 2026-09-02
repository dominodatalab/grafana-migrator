"""Report rendering, focused on the operator/api-neutral entry keys.

The point of `target_ref`/`matched_ref` is that one renderer serves both
backends. These tests pin that, and pin that operator output is unchanged when
nothing failed -- report.json is a documented artifact.
"""

from __future__ import annotations

import json

from grafana_migrator.report import MigrationReport


def test_defaults_to_the_operator_backend():
    assert MigrationReport().backend == "operator"
    assert json.loads(MigrationReport().to_json())["summary"]["backend"] == "operator"


def test_text_renders_cr_name_when_only_the_operator_key_is_present():
    r = MigrationReport()
    r.migrated.append({"uid": "d-1", "title": "CPU", "cr_name": "migrated-d-1"})
    assert "-> migrated-d-1" in r.to_text()


def test_text_renders_target_ref_for_a_backend_that_sets_no_cr_name():
    r = MigrationReport(backend="api")
    r.migrated.append({"uid": "d-1", "title": "CPU", "target_ref": "d-1"})
    out = r.to_text()
    assert "-> d-1" in out
    assert "backend                         : api" in out


def test_target_ref_wins_over_cr_name_when_both_are_written():
    r = MigrationReport()
    r.migrated.append({"uid": "d-1", "title": "CPU", "cr_name": "legacy", "target_ref": "preferred"})
    assert "-> preferred" in r.to_text()


def test_matched_ref_falls_back_to_matched_cr_name():
    r = MigrationReport()
    r.skipped_title_match.append({"uid": "d-1", "title": "CPU", "matched_cr_name": "other-cr"})
    assert "matches other-cr" in r.to_text()

    r2 = MigrationReport(backend="api")
    r2.skipped_title_match.append({"uid": "d-1", "title": "CPU", "matched_ref": "uid-on-target"})
    assert "matches uid-on-target" in r2.to_text()


def test_failures_and_warnings_are_absent_from_text_when_empty():
    # Operator output must not change shape just because api mode added fields.
    out = MigrationReport().to_text()
    assert "Failures:" not in out
    assert "Warnings:" not in out


def test_failures_and_warnings_render_when_present():
    r = MigrationReport(backend="api")
    r.warnings.append("contact point 'pd' secure field 'integrationKey' had no supplied value")
    r.failures.append(
        {"kind": "dashboard", "identity": {"uid": "d-9", "title": "Broken"}, "status": 400, "error": "bad panel"}
    )
    out = r.to_text()
    assert "Warnings:" in out
    assert "integrationKey" in out
    assert "Failures:" in out
    assert "dashboard 'Broken' [400]: bad panel" in out


def test_failure_label_falls_back_through_name_title_uid():
    r = MigrationReport(backend="api")
    r.failures.append({"kind": "contact_point", "identity": {"name": "pd"}, "error": "boom"})
    r.failures.append({"kind": "folder", "identity": {"uid": "f-1"}, "error": "boom"})
    r.failures.append({"kind": "dashboard", "identity": {}, "error": "boom"})
    out = r.to_text()
    assert "contact_point 'pd'" in out
    assert "folder 'f-1'" in out
    assert "dashboard '?'" in out


def test_summary_counts_failures_and_warnings():
    r = MigrationReport(backend="api")
    r.failures.append({"kind": "dashboard", "error": "x"})
    r.warnings.append("y")
    summary = json.loads(r.to_json())["summary"]
    assert summary["failures"] == 1
    assert summary["warnings"] == 1
