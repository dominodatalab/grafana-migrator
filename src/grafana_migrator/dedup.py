"""Decide, per source dashboard/folder, whether it already exists on the target.

Two signals, checked in order of confidence: uid match (strong -- stable
across re-provisioning) then normalized-title match (weak -- two different
dashboards can share a title, so it's skipped by default and only migrated
if the caller passes include_title_duplicates=True).
"""

from __future__ import annotations

from typing import Iterable

from .models import DedupDecision, ExistingAlertRuleGroup, ExistingContactPoint, ExistingDashboard, ExistingFolder
from .naming import normalize_title


class DashboardIndex:
    def __init__(self, existing: Iterable[ExistingDashboard]) -> None:
        self._by_uid: dict[str, ExistingDashboard] = {}
        self._by_title: dict[str, list[ExistingDashboard]] = {}
        for d in existing:
            if d.uid:
                self._by_uid[d.uid] = d
            if d.title:
                self._by_title.setdefault(normalize_title(d.title), []).append(d)

    def decide(self, uid: str, title: str, include_title_duplicates: bool = False) -> DedupDecision:
        uid_match = self._by_uid.get(uid)
        if uid_match:
            return DedupDecision(
                action="skip_uid_match",
                reason=f"uid {uid!r} already present as GrafanaDashboard/{uid_match.cr_name}",
                matched_cr_name=uid_match.cr_name,
            )

        title_matches = self._by_title.get(normalize_title(title))
        if title_matches and not include_title_duplicates:
            names = ", ".join(m.cr_name for m in title_matches)
            return DedupDecision(
                action="skip_title_match",
                reason=(
                    f"title {title!r} matches existing GrafanaDashboard(s) [{names}] under a "
                    "different uid -- likely the same dashboard re-imported; pass "
                    "--include-title-duplicates to migrate it anyway"
                ),
                matched_cr_name=title_matches[0].cr_name,
            )

        return DedupDecision(action="migrate", reason="no matching uid or title on target")


class FolderIndex:
    def __init__(self, existing: Iterable[ExistingFolder]) -> None:
        self._by_title: dict[str, ExistingFolder] = {}
        for f in existing:
            self._by_title[normalize_title(f.title)] = f

    def find(self, title: str) -> ExistingFolder | None:
        return self._by_title.get(normalize_title(title))


class AlertRuleIndex:
    """Dedup by rule uid only, flattened across every existing GrafanaAlertRuleGroup CR.

    No title fallback: alert rule uids are deterministic, human-derived ids
    (e.g. `PVUsageCritical_id`) that survive re-provisioning, unlike
    dashboard uids.
    """

    def __init__(self, existing_groups: Iterable[ExistingAlertRuleGroup]) -> None:
        self._by_uid: dict[str, ExistingAlertRuleGroup] = {}
        for group in existing_groups:
            for uid in group.rule_uids:
                self._by_uid[uid] = group

    def decide(self, uid: str, title: str) -> DedupDecision:
        match = self._by_uid.get(uid)
        if match:
            return DedupDecision(
                action="skip_uid_match",
                reason=f"uid {uid!r} already present in GrafanaAlertRuleGroup/{match.cr_name}",
                matched_cr_name=match.cr_name,
            )
        return DedupDecision(action="migrate", reason="no matching rule uid on target")


class ContactPointIndex:
    """Dedup by normalized contact point name -- mirrors FolderIndex.

    A contact point's `name` is how alert rules/routes address it, so two
    contact points with the same name are the same logical destination even
    if their underlying uid/settings differ.
    """

    def __init__(self, existing: Iterable[ExistingContactPoint]) -> None:
        self._by_name: dict[str, ExistingContactPoint] = {}
        for cp in existing:
            self._by_name[normalize_title(cp.name)] = cp

    def decide(self, name: str) -> DedupDecision:
        match = self._by_name.get(normalize_title(name))
        if match:
            return DedupDecision(
                action="skip_name_match",
                reason=f"name {name!r} already present as GrafanaContactPoint/{match.cr_name}",
                matched_cr_name=match.cr_name,
            )
        return DedupDecision(action="migrate", reason="no matching contact point name on target")
