import pytest

from grafana_migrator.naming import (
    alert_rule_group_cr_name,
    contact_point_cr_name,
    dashboard_cr_name,
    folder_cr_name,
    normalize_title,
    sanitize_k8s_name,
)


def test_sanitize_lowercases_and_strips_invalid_chars():
    assert sanitize_k8s_name("TEnL47Hnz") == "tenl47hnz"
    assert sanitize_k8s_name("e8c90978-07a2-485a-9312-f473a4193d16") == "e8c90978-07a2-485a-9312-f473a4193d16"


def test_sanitize_collapses_and_strips_dashes():
    assert sanitize_k8s_name("Sample Persistence Test") == "sample-persistence-test"
    assert sanitize_k8s_name("__weird___name__") == "weird-name"


def test_sanitize_empty_raises():
    with pytest.raises(ValueError):
        sanitize_k8s_name("___")


def test_sanitize_truncates_long_names():
    long_name = "a" * 300
    result = sanitize_k8s_name(long_name)
    assert len(result) <= 253


def test_dashboard_cr_name_is_deterministic_and_keyed_on_uid():
    assert dashboard_cr_name("antvsq") == dashboard_cr_name("antvsq")
    assert dashboard_cr_name("antvsq") == "migrated-antvsq"


def test_dashboard_cr_name_unaffected_by_title():
    # Same uid -> same name, regardless of what the caller passes as title elsewhere.
    assert dashboard_cr_name("afzndm") == "migrated-afzndm"


def test_folder_cr_name():
    assert folder_cr_name("Team Managed") == "migrated-team-managed"


def test_normalize_title_folds_case_and_whitespace():
    assert normalize_title("Team  Managed") == normalize_title("team managed")
    assert normalize_title("  Team Managed  ") == "team managed"


def test_alert_rule_group_cr_name_is_deterministic_and_keyed_on_folder_and_group():
    assert alert_rule_group_cr_name("Team Managed", "RabbitMQ") == alert_rule_group_cr_name("Team Managed", "RabbitMQ")
    assert alert_rule_group_cr_name("Team Managed", "RabbitMQ") == "migrated-team-managed-rabbitmq"


def test_contact_point_cr_name():
    assert contact_point_cr_name("Platform Slack") == "migrated-platform-slack"
