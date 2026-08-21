from grafana_migrator.dedup import AlertRuleIndex, ContactPointIndex, DashboardIndex, FolderIndex
from grafana_migrator.models import ExistingAlertRuleGroup, ExistingContactPoint, ExistingDashboard, ExistingFolder


def test_uid_match_wins_over_no_match():
    index = DashboardIndex(
        [
            ExistingDashboard(
                cr_name="my-grafana-elasticsearch", namespace="ns", uid="ElasticSearch", title="ElasticSearch"
            )
        ]
    )
    decision = index.decide(uid="ElasticSearch", title="ElasticSearch")
    assert decision.action == "skip_uid_match"
    assert decision.matched_cr_name == "my-grafana-elasticsearch"


def test_new_uid_and_title_migrates():
    index = DashboardIndex(
        [
            ExistingDashboard(
                cr_name="my-grafana-elasticsearch", namespace="ns", uid="ElasticSearch", title="ElasticSearch"
            )
        ]
    )
    decision = index.decide(uid="antvsq", title="Sample Persistence Test Dashboard")
    assert decision.action == "migrate"


def test_title_collision_with_different_uid_is_flagged_not_migrated_by_default():
    index = DashboardIndex(
        [
            ExistingDashboard(
                cr_name="my-grafana-elasticsearch", namespace="ns", uid="ElasticSearch", title="ElasticSearch"
            )
        ]
    )
    # Same title, different uid -- e.g. dashboard cloned/re-imported on the source side.
    decision = index.decide(uid="some-other-uid", title="elasticsearch")
    assert decision.action == "skip_title_match"
    assert decision.matched_cr_name == "my-grafana-elasticsearch"


def test_title_collision_can_be_forced_through():
    index = DashboardIndex(
        [
            ExistingDashboard(
                cr_name="my-grafana-elasticsearch", namespace="ns", uid="ElasticSearch", title="ElasticSearch"
            )
        ]
    )
    decision = index.decide(uid="some-other-uid", title="ElasticSearch", include_title_duplicates=True)
    assert decision.action == "migrate"


def test_dashboard_with_no_title_or_uid_recorded_never_matches():
    index = DashboardIndex([ExistingDashboard(cr_name="broken", namespace="ns", uid=None, title=None)])
    decision = index.decide(uid="antvsq", title="Sample Persistence Test Dashboard")
    assert decision.action == "migrate"


def test_folder_index_matches_by_normalized_title():
    index = FolderIndex([ExistingFolder(cr_name="team-managed", namespace="ns", title="Team Managed")])
    for probe in ("team managed", "  Team   Managed "):
        match = index.find(probe)
        assert match is not None
        assert match.cr_name == "team-managed"


def test_folder_index_returns_none_for_unknown_title():
    index = FolderIndex([ExistingFolder(cr_name="team-managed", namespace="ns", title="Team Managed")])
    assert index.find("Sample Persistence Test") is None


def test_alert_rule_index_matches_by_uid_flattened_across_groups():
    index = AlertRuleIndex(
        [
            ExistingAlertRuleGroup(
                cr_name="my-grafana-rabbitmq",
                namespace="ns",
                folder_ref="team-managed",
                rule_group="RabbitMQ",
                rule_uids=("RabbitMQMeomoryHigh_id", "RabbitMQFileDescriptorsUsage_id"),
            )
        ]
    )
    decision = index.decide(uid="RabbitMQMeomoryHigh_id", title="RabbitMQ High Memory Usage")
    assert decision.action == "skip_uid_match"
    assert decision.matched_cr_name == "my-grafana-rabbitmq"


def test_alert_rule_index_new_uid_migrates():
    index = AlertRuleIndex(
        [
            ExistingAlertRuleGroup(
                cr_name="my-grafana-rabbitmq",
                namespace="ns",
                folder_ref="team-managed",
                rule_group="RabbitMQ",
                rule_uids=("RabbitMQMeomoryHigh_id",),
            )
        ]
    )
    decision = index.decide(uid="SomeNewRule_id", title="Some New Rule")
    assert decision.action == "migrate"


def test_contact_point_index_matches_by_normalized_name():
    index = ContactPointIndex(
        [ExistingContactPoint(cr_name="migrated-platform-slack", namespace="ns", name="Platform Slack")]
    )
    decision = index.decide("platform slack")
    assert decision.action == "skip_name_match"
    assert decision.matched_cr_name == "migrated-platform-slack"


def test_contact_point_index_new_name_migrates():
    index = ContactPointIndex(
        [ExistingContactPoint(cr_name="migrated-platform-slack", namespace="ns", name="Platform Slack")]
    )
    decision = index.decide("PagerDuty Escalation")
    assert decision.action == "migrate"
