"""The --secrets-file loader, validator and skeleton generator."""

from __future__ import annotations

import json

import pytest

from grafana_migrator.models import SourceContactPoint
from grafana_migrator.secrets_file import (
    SecretsFileError,
    load_secrets_file,
    secrets_for,
    secrets_skeleton,
    validate_secrets,
)

PAGERDUTY = SourceContactPoint(
    uid="1", name="critical-pagerduty", type="pagerduty", settings={}, secure_field_names=("integrationKey",)
)
SLACK = SourceContactPoint(uid="2", name="Ops Slack", type="slack", settings={}, secure_field_names=("url", "token"))
EMAIL = SourceContactPoint(uid="3", name="Team Email", type="email", settings={"addresses": "a@b.com"})


def _write(tmp_path, name, text):
    p = tmp_path / name
    p.write_text(text)
    return p


def test_loads_yaml(tmp_path):
    p = _write(tmp_path, "s.yaml", 'critical-pagerduty:\n  integrationKey: "abc"\n')
    assert load_secrets_file(p) == {"critical-pagerduty": {"integrationKey": "abc"}}


def test_loads_json_too(tmp_path):
    # YAML is a JSON superset, so one loader covers both.
    p = _write(tmp_path, "s.json", json.dumps({"critical-pagerduty": {"integrationKey": "abc"}}))
    assert load_secrets_file(p) == {"critical-pagerduty": {"integrationKey": "abc"}}


def test_keys_are_normalized_like_dedup_does(tmp_path):
    # ContactPointIndex matches on normalized names, so this file must too.
    p = _write(tmp_path, "s.yaml", '"  Critical   PagerDuty ":\n  integrationKey: "abc"\n')
    loaded = load_secrets_file(p)
    assert secrets_for(loaded, "critical pagerduty") == {"integrationKey": "abc"}
    assert secrets_for(loaded, "CRITICAL PAGERDUTY") == {"integrationKey": "abc"}


def test_numeric_looking_values_are_coerced_to_strings(tmp_path):
    p = _write(tmp_path, "s.yaml", "cp:\n  integrationKey: 1234567890\n")
    assert load_secrets_file(p)["cp"]["integrationKey"] == "1234567890"


def test_null_value_becomes_empty_string_not_none(tmp_path):
    p = _write(tmp_path, "s.yaml", "cp:\n  integrationKey:\n")
    assert load_secrets_file(p)["cp"]["integrationKey"] == ""


def test_empty_file_is_an_empty_mapping(tmp_path):
    assert load_secrets_file(_write(tmp_path, "s.yaml", "")) == {}


def test_missing_file_is_a_clean_error(tmp_path):
    with pytest.raises(SecretsFileError) as excinfo:
        load_secrets_file(tmp_path / "nope.yaml")
    assert "cannot read" in str(excinfo.value)


def test_invalid_yaml_is_a_clean_error(tmp_path):
    with pytest.raises(SecretsFileError) as excinfo:
        load_secrets_file(_write(tmp_path, "s.yaml", "key: [unclosed\n"))
    assert "not valid YAML" in str(excinfo.value)


def test_top_level_list_is_rejected_with_the_expected_shape(tmp_path):
    with pytest.raises(SecretsFileError) as excinfo:
        load_secrets_file(_write(tmp_path, "s.yaml", "- a\n- b\n"))
    assert "must be a mapping" in str(excinfo.value)


def test_scalar_entry_is_rejected(tmp_path):
    # A likely mistake: one level of nesting forgotten.
    with pytest.raises(SecretsFileError) as excinfo:
        load_secrets_file(_write(tmp_path, "s.yaml", "critical-pagerduty: abc123\n"))
    assert "field -> value" in str(excinfo.value)


def test_secrets_for_returns_empty_when_absent():
    assert secrets_for({}, "anything") == {}


def test_secrets_for_returns_a_copy():
    loaded = {"cp": {"a": "b"}}
    got = secrets_for(loaded, "cp")
    got["a"] = "mutated"
    assert loaded["cp"]["a"] == "b"


# ---------------------------------------------------------------------------
# validation -- a typo here is otherwise invisible
# ---------------------------------------------------------------------------


def test_no_warnings_when_the_file_matches():
    assert validate_secrets({"critical-pagerduty": {"integrationKey": "x"}}, [PAGERDUTY]) == []


def test_warns_about_an_entry_for_a_contact_point_not_being_imported():
    warnings = validate_secrets({"nope": {"integrationKey": "x"}}, [PAGERDUTY])
    assert len(warnings) == 1
    assert "not a contact point being imported" in warnings[0]


def test_warns_about_a_field_the_contact_point_does_not_have():
    warnings = validate_secrets({"critical-pagerduty": {"wrongField": "x"}}, [PAGERDUTY])
    assert len(warnings) == 1
    assert "no such secure field" in warnings[0]
    assert "integrationKey" in warnings[0]


def test_warns_about_a_field_on_a_contact_point_with_no_secure_fields():
    warnings = validate_secrets({"team email": {"password": "x"}}, [EMAIL])
    assert "none" in warnings[0]


# ---------------------------------------------------------------------------
# skeleton
# ---------------------------------------------------------------------------


def test_skeleton_covers_exactly_the_redacted_fields():
    out = secrets_skeleton([PAGERDUTY, SLACK, EMAIL])
    assert '"critical-pagerduty":' in out
    assert "  integrationKey: \"\"" in out
    assert '"Ops Slack":' in out
    assert "  url: \"\"" in out
    assert "  token: \"\"" in out
    # a contact point with nothing redacted has nothing to fill in
    assert "Team Email" not in out


def test_skeleton_round_trips_through_the_loader(tmp_path):
    p = _write(tmp_path, "s.yaml", secrets_skeleton([PAGERDUTY, SLACK]))
    loaded = load_secrets_file(p)
    assert loaded == {"critical-pagerduty": {"integrationKey": ""}, "ops slack": {"url": "", "token": ""}}


def test_skeleton_quotes_names_with_awkward_characters(tmp_path):
    awkward = SourceContactPoint(
        uid="9", name="Ops: Slack #alerts", type="slack", settings={}, secure_field_names=("url",)
    )
    p = _write(tmp_path, "s.yaml", secrets_skeleton([awkward]))
    assert load_secrets_file(p) == {"ops: slack #alerts": {"url": ""}}


def test_skeleton_is_still_valid_yaml_when_nothing_needs_secrets(tmp_path):
    p = _write(tmp_path, "s.yaml", secrets_skeleton([EMAIL]))
    assert load_secrets_file(p) == {}
    assert "nothing to fill in" in p.read_text()


def test_skeleton_says_to_keep_it_out_of_version_control():
    assert "out of version control" in secrets_skeleton([PAGERDUTY])
