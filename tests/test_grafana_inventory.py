"""Target inventory read over HTTP, and its parity with the kubectl one."""

from __future__ import annotations

from typing import Any, cast

import requests
from fake_session import FakeResponse, FakeSession

from grafana_migrator import grafana_inventory, k8s_inventory
from grafana_migrator.grafana_client import GrafanaClient
from grafana_migrator.import_plan import (
    POLICY_CUSTOM,
    POLICY_DEFAULT,
    POLICY_PROVISIONED,
    PlanOptions,
    plan_import,
)
from grafana_migrator.report import MigrationReport
from grafana_migrator.source_dump import SourceDump

SEARCH = [
    {"type": "dash-folder", "uid": "f-1", "title": "Platform Team"},
    {"type": "dash-db", "uid": "d-1", "title": "CPU", "folderUid": "f-1"},
    {"type": "dash-db", "uid": "d-2", "title": "Memory"},
    {"type": "dash-db", "title": "No Uid Row"},  # defensive: skipped, not a crash
]
RULES = [
    {"uid": "r-1", "ruleGroup": "G", "folderUID": "f-1", "title": "A"},
    {"uid": "r-2", "ruleGroup": "G", "folderUID": "f-1", "title": "B"},
    {"uid": "r-3", "ruleGroup": "H", "folderUID": "f-1", "title": "C"},
]
CONTACT_POINTS = [{"uid": "cp-1", "name": "Platform Slack", "type": "slack"}]
DEFAULT_TREE = {"receiver": "empty", "group_by": ["grafana_folder"]}
CUSTOM_TREE = {"receiver": "Platform Slack", "routes": [{"receiver": "Platform Slack"}]}


def _client(tree: dict[str, Any] | None = None) -> GrafanaClient:
    session = FakeSession(
        {
            ("GET", "/api/search"): FakeResponse(200, SEARCH),
            ("GET", "/api/v1/provisioning/alert-rules"): FakeResponse(200, RULES),
            ("GET", "/api/v1/provisioning/contact-points"): FakeResponse(200, CONTACT_POINTS),
            ("GET", "/api/v1/provisioning/policies"): FakeResponse(200, tree if tree is not None else DEFAULT_TREE),
        }
    )
    return GrafanaClient("http://graf.test", token="t", session=cast(requests.Session, session))


def test_one_search_call_yields_both_dashboards_and_folders():
    client = _client()
    dashboards, folders = grafana_inventory.list_existing_dashboards_and_folders(client)
    assert [(d.uid, d.title) for d in dashboards] == [("d-1", "CPU"), ("d-2", "Memory")]
    assert [(f.uid, f.title) for f in folders] == [("f-1", "Platform Team")]
    # cr_name is the opaque target ref; over HTTP that is the Grafana uid
    assert [d.cr_name for d in dashboards] == ["d-1", "d-2"]
    assert folders[0].cr_name == "f-1"
    # exactly one request, not one per kind
    assert client.session.paths("GET") == ["/api/search"]  # type: ignore[attr-defined]


def test_flat_rule_list_is_grouped_into_folder_group_units():
    groups = grafana_inventory.list_existing_alert_rule_groups(_client())
    by_group = {g.rule_group: g for g in groups}
    assert set(by_group) == {"G", "H"}
    assert by_group["G"].rule_uids == ("r-1", "r-2")
    assert by_group["H"].rule_uids == ("r-3",)
    assert by_group["G"].cr_name == "f-1/G"


def test_contact_points_are_keyed_by_name_for_dedup():
    cps = grafana_inventory.list_existing_contact_points(_client())
    assert [(c.name, c.cr_name) for c in cps] == [("Platform Slack", "cp-1")]


def test_policy_state_distinguishes_default_custom_and_provisioned():
    assert grafana_inventory.notification_policy_state(_client(DEFAULT_TREE)) == POLICY_DEFAULT
    assert grafana_inventory.notification_policy_state(_client(CUSTOM_TREE)) == POLICY_CUSTOM
    provisioned = dict(CUSTOM_TREE, provenance="file")
    assert grafana_inventory.notification_policy_state(_client(provisioned)) == POLICY_PROVISIONED


def test_skip_alerts_means_the_alerting_endpoints_are_never_called():
    client = _client()
    grafana_inventory.build_target_inventory(client, include_alerting=False)
    assert client.session.paths("GET") == ["/api/search"]  # type: ignore[attr-defined]


def test_full_inventory_is_three_calls_and_the_policy_probe_stays_lazy():
    client = _client()
    inventory = grafana_inventory.build_target_inventory(client, include_alerting=True)
    assert client.session.paths("GET") == [  # type: ignore[attr-defined]
        "/api/search",
        "/api/v1/provisioning/alert-rules",
        "/api/v1/provisioning/contact-points",
    ]
    assert inventory.probe_notification_policy_state() == POLICY_DEFAULT
    assert "/api/v1/provisioning/policies" in client.session.paths("GET")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# parity: the same logical target, read two ways, must plan identically
# ---------------------------------------------------------------------------

DUMP = SourceDump(
    search_results=[
        {"uid": "f-1", "title": "Platform Team", "type": "dash-folder"},
        {"uid": "d-1", "title": "CPU", "type": "dash-db", "folderUid": "f-1"},
        {"uid": "d-9", "title": "Brand New", "type": "dash-db"},
        {"uid": "d-8", "title": "Memory", "type": "dash-db"},
    ],
    dashboards_by_uid={
        "d-1": {"dashboard": {"uid": "d-1"}, "meta": {}},
        "d-9": {"dashboard": {"uid": "d-9"}, "meta": {}},
        "d-8": {"dashboard": {"uid": "d-8"}, "meta": {}},
    },
    alert_rules_raw=[
        {"uid": "r-1", "title": "A", "ruleGroup": "G", "folderUID": "f-1", "condition": "A", "data": []},
        {"uid": "r-new", "title": "New", "ruleGroup": "G", "folderUID": "f-1", "condition": "A", "data": []},
    ],
    contact_points_raw=[{"uid": "x", "name": "Platform Slack", "type": "slack", "settings": {"a": "b"}}],
    notification_policy_raw=CUSTOM_TREE,
)

# The same target expressed as operator CRs.
KUBECTL_TARGET = {
    "grafanadashboards.grafana.integreatly.org": [
        {"metadata": {"name": "cr-d-1"}, "spec": {"json": '{"uid": "d-1", "title": "CPU"}'}},
        {"metadata": {"name": "cr-d-2"}, "spec": {"json": '{"uid": "d-2", "title": "Memory"}'}},
    ],
    "grafanafolders.grafana.integreatly.org": [{"metadata": {"name": "cr-f-1"}, "spec": {"title": "Platform Team"}}],
    "grafanaalertrulegroups.grafana.integreatly.org": [
        {"metadata": {"name": "cr-g"}, "spec": {"name": "G", "folderRef": "cr-f-1", "rules": [{"uid": "r-1"}]}}
    ],
    "grafanacontactpoints.grafana.integreatly.org": [
        {"metadata": {"name": "cr-cp"}, "spec": {"name": "Platform Slack"}}
    ],
    "grafananotificationpolicies.grafana.integreatly.org": [{"metadata": {"name": "np"}}],
}


def test_kubectl_and_http_inventories_produce_the_same_plan(monkeypatch):
    """The property that keeps the two backends honest.

    Same snapshot, same logical target, read through kubectl CRs in one case
    and Grafana's API in the other -- every migrate/skip decision must match.
    Only the opaque refs differ (CR name vs uid), so this asserts on decisions.
    """
    monkeypatch.setattr(
        k8s_inventory,
        "_kubectl_get_json",
        lambda resource, namespace, context: {"items": KUBECTL_TARGET.get(resource, [])},
    )
    op_report = MigrationReport(backend="operator")
    op_plan = plan_import(
        DUMP,
        k8s_inventory.build_target_inventory("ns", None, include_alerting=True),
        PlanOptions(),
        op_report,
    )

    api_report = MigrationReport(backend="api")
    api_plan = plan_import(
        DUMP,
        grafana_inventory.build_target_inventory(_client(CUSTOM_TREE), include_alerting=True),
        PlanOptions(),
        api_report,
    )

    assert [d.uid for d, _ in op_plan.dashboards_new] == [d.uid for d, _ in api_plan.dashboards_new]
    assert [f.uid for f in op_plan.folders_new] == [f.uid for f in api_plan.folders_new]
    assert [f.uid for f, _ in op_plan.folders_existing] == [f.uid for f, _ in api_plan.folders_existing]
    assert [(u.folder_uid, u.rule_group, [r.uid for r in u.rules]) for u in op_plan.rule_groups_new] == [
        (u.folder_uid, u.rule_group, [r.uid for r in u.rules]) for u in api_plan.rule_groups_new
    ]
    assert [c.name for c in op_plan.contact_points_new] == [c.name for c in api_plan.contact_points_new]
    assert (op_plan.notification_policy is None) == (api_plan.notification_policy is None)

    for field_name in (
        "skipped_uid_match",
        "skipped_title_match",
        "alert_rules_skipped_uid_match",
        "contact_points_skipped_name_match",
        "contact_points_skipped_default",
    ):
        op_ids = [e.get("uid") for e in getattr(op_report, field_name)]
        api_ids = [e.get("uid") for e in getattr(api_report, field_name)]
        assert op_ids == api_ids, field_name
    assert op_report.notification_policy_status == api_report.notification_policy_status
