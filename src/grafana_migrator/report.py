"""Aggregate and render a summary of what the exporter did and why."""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class MigrationReport:
    migrated: list[dict] = field(default_factory=list)
    skipped_uid_match: list[dict] = field(default_factory=list)
    skipped_title_match: list[dict] = field(default_factory=list)
    folders_created: list[dict] = field(default_factory=list)
    folders_reused: list[dict] = field(default_factory=list)

    alert_rules_migrated: list[dict] = field(default_factory=list)
    alert_rules_skipped_uid_match: list[dict] = field(default_factory=list)

    contact_points_migrated: list[dict] = field(default_factory=list)
    contact_points_skipped_name_match: list[dict] = field(default_factory=list)
    contact_points_skipped_default: list[dict] = field(default_factory=list)

    notification_policy_status: str = "not_attempted"  # migrated | skipped_default | skipped_target_has_policy
    notification_policy_detail: str = ""

    def to_dict(self) -> dict:
        return {
            "summary": {
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
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=False)

    def to_text(self) -> str:
        s = self.to_dict()["summary"]
        lines = [
            "grafana-migrator report",
            "================================",
            f"  dashboards discovered on source : {s['dashboards_discovered']}",
            f"  migrated (new manifests written): {s['dashboards_migrated']}",
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
                lines.append(f"  - {m['title']!r} (uid={m['uid']}) -> {m['cr_name']}")
        if self.skipped_title_match:
            lines.append("\nTitle-collision skips (review manually if unexpected):")
            for m in self.skipped_title_match:
                lines.append(f"  - {m['title']!r} (uid={m['uid']}) matches {m['matched_cr_name']}")
        if self.alert_rules_migrated:
            lines.append("\nMigrated alert rules:")
            for m in self.alert_rules_migrated:
                lines.append(f"  - {m['title']!r} (uid={m['uid']}, group={m['rule_group']!r}) -> {m['cr_name']}")
        if self.contact_points_migrated:
            lines.append("\nMigrated contact points (secrets need populating -- see report detail):")
            for m in self.contact_points_migrated:
                secret_note = f", secret={m['secret_name']!r}" if m.get("secure_field_names") else ""
                lines.append(f"  - {m['name']!r} (type={m['type']}) -> {m['cr_name']}{secret_note}")
        if self.notification_policy_detail:
            lines.append(f"\nNotification policy: {self.notification_policy_detail}")
        return "\n".join(lines)
