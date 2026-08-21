from pathlib import Path
from typing import Any

import pytest

from grafana_migrator.source_dump import SourceDump, SourceDumpError, read_source_dump, write_source_dump

SEARCH_RESULTS = [
    {"uid": "folder-1", "title": "Team Alerts", "type": "dash-folder"},
    {
        "uid": "dash-1",
        "title": "CPU Overview",
        "type": "dash-db",
        "folderUid": "folder-1",
        "folderTitle": "Team Alerts",
    },
]
DASHBOARD_PAYLOAD = {"dashboard": {"uid": "dash-1", "title": "CPU Overview", "panels": []}, "meta": {"version": 3}}


def _sample_dump(**overrides) -> SourceDump:
    fields: dict[str, Any] = dict(
        search_results=SEARCH_RESULTS,
        dashboards_by_uid={"dash-1": DASHBOARD_PAYLOAD},
        alert_rules_raw=[{"uid": "rule-1"}],
        contact_points_raw=[{"uid": "cp-1"}],
        notification_policy_raw={"receiver": "empty"},
    )
    fields.update(overrides)
    return SourceDump(**fields)


def test_round_trip_preserves_every_field(tmp_path):
    write_source_dump(_sample_dump(), tmp_path)
    result = read_source_dump(tmp_path)
    assert result.search_results == SEARCH_RESULTS
    assert result.dashboards_by_uid == {"dash-1": DASHBOARD_PAYLOAD}
    assert result.alert_rules_raw == [{"uid": "rule-1"}]
    assert result.contact_points_raw == [{"uid": "cp-1"}]
    assert result.notification_policy_raw == {"receiver": "empty"}


def test_round_trip_distinguishes_not_fetched_from_empty(tmp_path):
    # alert_rules_raw=[] means "fetched, and there were none"; None means
    # "never fetched" (e.g. --skip-alerts at export time). These must not
    # collapse into the same on-disk representation.
    write_source_dump(
        _sample_dump(alert_rules_raw=None, contact_points_raw=None, notification_policy_raw=None), tmp_path
    )
    result = read_source_dump(tmp_path)
    assert result.alert_rules_raw is None
    assert result.contact_points_raw is None
    assert result.notification_policy_raw is None


def test_round_trip_empty_but_fetched_alert_rules(tmp_path):
    write_source_dump(_sample_dump(alert_rules_raw=[]), tmp_path)
    result = read_source_dump(tmp_path)
    assert result.alert_rules_raw == []


def test_read_source_dump_rejects_directory_missing_search_json(tmp_path):
    with pytest.raises(SourceDumpError):
        read_source_dump(tmp_path)


def test_read_source_dump_on_nonexistent_directory_raises():
    with pytest.raises(SourceDumpError):
        read_source_dump(Path("/no/such/directory/anywhere"))


def test_write_source_dump_writes_one_file_per_dashboard(tmp_path):
    write_source_dump(_sample_dump(), tmp_path)
    assert (tmp_path / "dashboards" / "dash-1.json").is_file()
    assert (tmp_path / "search.json").is_file()
    assert (tmp_path / "alert-rules.json").is_file()
    assert (tmp_path / "contact-points.json").is_file()
    assert (tmp_path / "notification-policy.json").is_file()
    assert (tmp_path / "meta.json").is_file()
