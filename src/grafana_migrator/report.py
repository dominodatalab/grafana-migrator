"""Aggregate and render a summary of what an import did and why.

One report type serves both backends. Per-item entries carry a neutral
`target_ref` / `matched_ref` -- the CR name in operator mode, the Grafana uid
in api mode -- so the renderer does not have to know which backend ran.
Operator mode also keeps writing the original `cr_name` / `matched_cr_name`
keys, since report.json is a documented artifact that docs/ADVANCED.md names
by key and there are reports on disk from earlier runs.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


def _ref(entry: dict[str, Any]) -> Any:
    """The thing this entry landed as on the target, whichever backend wrote it."""
    return entry.get("target_ref") or entry.get("cr_name")


def _matched(entry: dict[str, Any]) -> Any:
    """The pre-existing target object this entry was deduped against."""
    return entry.get("matched_ref") or entry.get("matched_cr_name")


@dataclass
class MigrationReport:
    backend: str = "operator"  # operator | api

    migrated: list[dict[str, Any]] = field(default_factory=list)
    skipped_uid_match: list[dict[str, Any]] = field(default_factory=list)
    skipped_title_match: list[dict[str, Any]] = field(default_factory=list)
    folders_created: list[dict[str, Any]] = field(default_factory=list)
    folders_reused: list[dict[str, Any]] = field(default_factory=list)

    alert_rules_migrated: list[dict[str, Any]] = field(default_factory=list)
    alert_rules_skipped_uid_match: list[dict[str, Any]] = field(default_factory=list)

    contact_points_migrated: list[dict[str, Any]] = field(default_factory=list)
    contact_points_skipped_name_match: list[dict[str, Any]] = field(default_factory=list)
    contact_points_skipped_default: list[dict[str, Any]] = field(default_factory=list)

    # not_attempted | migrated | skipped_by_flag | skipped_unavailable | skipped_default
    # | skipped_target_has_policy | skipped_target_policy_provisioned | failed
    notification_policy_status: str = "not_attempted"
    notification_policy_detail: str = ""

    # api mode only: a write that failed, and anything the run wants to warn
    # about without failing (e.g. a secure field with no supplied value).
    failures: list[dict[str, Any]] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "summary": {
                "backend": self.backend,
                "dashboards_discovered": (
                    len(self.migrated) + len(self.skipped_uid_match) + len(self.skipped_title_match)
                ),
                "dashboards_migrated": len(self.migrated),
                "dashboards_skipped_uid_match": len(self.skipped_uid_match),
                "dashboards_skipped_title_match": len(self.skipped_title_match),
                "folders_created": len(self.folders_created),
                "folders_reused": len(self.folders_reused),
                "alert_rules_discovered": len(self.alert_rules_migrated) + len(self.alert_rules_skipped_uid_match),
                "alert_rules_migrated": len(self.alert_rules_migrated),
                "alert_rules_skipped_uid_match": len(self.alert_rules_skipped_uid_match),
                "contact_points_migrated": len(self.contact_points_migrated),
                "contact_points_skipped_name_match": len(self.contact_points_skipped_name_match),
                "contact_points_skipped_default": len(self.contact_points_skipped_default),
                "notification_policy_status": self.notification_policy_status,
                "failures": len(self.failures),
                "warnings": len(self.warnings),
            },
            "migrated": self.migrated,
            "skipped_uid_match": self.skipped_uid_match,
            "skipped_title_match": self.skipped_title_match,
            "folders_created": self.folders_created,
            "folders_reused": self.folders_reused,
            "alert_rules_migrated": self.alert_rules_migrated,
            "alert_rules_skipped_uid_match": self.alert_rules_skipped_uid_match,
            "contact_points_migrated": self.contact_points_migrated,
            "contact_points_skipped_name_match": self.contact_points_skipped_name_match,
            "contact_points_skipped_default": self.contact_points_skipped_default,
            "notification_policy_status": self.notification_policy_status,
            "notification_policy_detail": self.notification_policy_detail,
            "failures": self.failures,
            "warnings": self.warnings,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)

    def to_text(self) -> str:
        s = self.to_dict()["summary"]
        lines = [
            "grafana-migrator report",
            "================================",
            f"  backend                         : {s['backend']}",
            f"  dashboards discovered on source : {s['dashboards_discovered']}",
            f"  migrated                        : {s['dashboards_migrated']}",
            f"  skipped (uid already on target) : {s['dashboards_skipped_uid_match']}",
            f"  skipped (title collision only)  : {s['dashboards_skipped_title_match']}",
            f"  folders created                 : {s['folders_created']}",
            f"  folders reused (matched by title): {s['folders_reused']}",
            "",
            f"  alert rules discovered on source: {s['alert_rules_discovered']}",
            f"  alert rules migrated (new rules) : {s['alert_rules_migrated']}",
            f"  alert rules skipped (uid match)  : {s['alert_rules_skipped_uid_match']}",
            f"  contact points migrated          : {s['contact_points_migrated']}",
            f"  contact points skipped (name match): {s['contact_points_skipped_name_match']}",
            f"  contact points skipped (default) : {s['contact_points_skipped_default']}",
            f"  notification policy              : {s['notification_policy_status']}",
        ]
        if self.migrated:
            lines.append("\nMigrated dashboards:")
            for m in self.migrated:
                lines.append(f"  - {m['title']!r} (uid={m['uid']}) -> {_ref(m)}")
        if self.skipped_title_match:
            lines.append("\nTitle-collision skips (review manually if unexpected):")
            for m in self.skipped_title_match:
                lines.append(f"  - {m['title']!r} (uid={m['uid']}) matches {_matched(m)}")
        if self.alert_rules_migrated:
            lines.append("\nMigrated alert rules:")
            for m in self.alert_rules_migrated:
                lines.append(f"  - {m['title']!r} (uid={m['uid']}, group={m['rule_group']!r}) -> {_ref(m)}")
        if self.contact_points_migrated:
            lines.append("\nMigrated contact points (secrets need populating -- see report detail):")
            for m in self.contact_points_migrated:
                secret_note = f", secret={m['secret_name']!r}" if m.get("secure_field_names") else ""
                lines.append(f"  - {m['name']!r} (type={m['type']}) -> {_ref(m)}{secret_note}")
        if self.notification_policy_detail:
            lines.append(f"\nNotification policy: {self.notification_policy_detail}")
        if self.warnings:
            lines.append("\nWarnings:")
            for w in self.warnings:
                lines.append(f"  - {w}")
        if self.failures:
            lines.append("\nFailures:")
            for f_ in self.failures:
                identity = f_.get("identity") or {}
                label = identity.get("name") or identity.get("title") or identity.get("uid") or "?"
                status = f_.get("status")
                status_note = f" [{status}]" if status else ""
                lines.append(f"  - {f_.get('kind', '?')} {label!r}{status_note}: {f_.get('error', '')}")
        return "\n".join(lines)
