"""Plain data structures shared across grafana_migrator modules."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class SourceFolder:
    uid: str
    title: str


@dataclass(frozen=True)
class SourceDashboardRef:
    """One row from the source Grafana instance's /api/search (type=dash-db)."""

    uid: str
    title: str
    folder_uid: Optional[str]
    folder_title: Optional[str]


@dataclass(frozen=True)
class ExistingDashboard:
    """A GrafanaDashboard CR already on the target cluster."""

    cr_name: str
    namespace: str
    uid: Optional[str]
    title: Optional[str]


@dataclass(frozen=True)
class ExistingFolder:
    """A folder that already exists on the target.

    `cr_name` is the opaque identifier of that folder on the target: the
    GrafanaFolder CR name in operator mode. `uid` is Grafana's own folder uid,
    which only the HTTP path can see -- the CR does not carry it.
    """

    cr_name: str
    namespace: str
    title: str
    uid: Optional[str] = None


@dataclass(frozen=True)
class DedupDecision:
    """Whether a source dashboard should be migrated, and why."""

    action: str  # "migrate" | "skip_uid_match" | "skip_title_match"
    reason: str
    matched_cr_name: Optional[str] = None


@dataclass(frozen=True)
class SourceAlertRule:
    """One row from the source Grafana instance's GET /api/v1/provisioning/alert-rules."""

    uid: str
    title: str
    rule_group: str
    folder_uid: str
    condition: str
    data: list[dict[str, Any]]
    no_data_state: str
    exec_err_state: str
    for_: str  # Go duration string, e.g. "15m0s" -- as returned by the provisioning API
    annotations: dict[str, str] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)
    is_paused: bool = False
    notification_settings: Optional[dict[str, Any]] = None
    dashboard_uid: Optional[str] = None
    panel_id: Optional[int] = None
    record: Optional[dict[str, Any]] = None
    keep_firing_for: Optional[str] = None


@dataclass(frozen=True)
class ExistingAlertRuleGroup:
    """A GrafanaAlertRuleGroup CR already on the target cluster."""

    cr_name: str
    namespace: str
    folder_ref: Optional[str]
    rule_group: Optional[str]
    rule_uids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SourceContactPoint:
    """One receiver from the source Grafana instance's GET /api/v1/provisioning/contact-points.

    `settings` holds only non-secure values; `secure_field_names` records
    which secure keys exist on the source (never their values).
    """

    uid: str
    name: str
    type: str
    settings: dict[str, Any] = field(default_factory=dict)
    secure_field_names: tuple[str, ...] = ()
    disable_resolve_message: bool = False


@dataclass(frozen=True)
class ExistingContactPoint:
    """A GrafanaContactPoint CR already on the target cluster."""

    cr_name: str
    namespace: str
    name: str


@dataclass(frozen=True)
class SourceNotificationPolicy:
    """The single alertmanager route tree from GET /api/v1/provisioning/policies."""

    route: dict[str, Any]
