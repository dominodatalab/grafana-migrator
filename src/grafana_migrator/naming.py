"""Deterministic, idempotent Kubernetes-name generation.

Sanitizes to a DNS-1123 subdomain (lowercase alphanumeric, '-', '.', max 253
chars) so re-running the tool produces the same CR name for the same source
dashboard/folder, keeping `kubectl apply` idempotent.
"""

from __future__ import annotations

import re

_INVALID_CHARS = re.compile(r"[^a-z0-9-]+")
_DASH_COLLAPSE = re.compile(r"-+")
MAX_NAME_LEN = 253


def _slugify(raw: str) -> str:
    lowered = raw.strip().lower()
    slug = _INVALID_CHARS.sub("-", lowered)
    slug = _DASH_COLLAPSE.sub("-", slug)
    return slug.strip("-")


def sanitize_k8s_name(raw: str, max_len: int = MAX_NAME_LEN) -> str:
    slug = _slugify(raw)
    if not slug:
        raise ValueError(f"cannot derive a valid k8s name from {raw!r}")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-")
    return slug


def dashboard_cr_name(uid: str, prefix: str = "migrated") -> str:
    """CR name for a migrated dashboard, keyed by the *source* Grafana UID.

    Keying on the source uid (not the title) is what makes re-running the
    exporter safe: title edits on either side don't change the derived name.
    """
    return sanitize_k8s_name(f"{prefix}-{uid}")


def folder_cr_name(title: str, prefix: str = "migrated") -> str:
    return sanitize_k8s_name(f"{prefix}-{title}")


def alert_rule_group_cr_name(folder_title: str, rule_group: str, prefix: str = "migrated") -> str:
    """CR name for a migrated alert rule group, keyed by the *source* folder + rule group.

    Unlike dashboards (one CR per uid), a GrafanaAlertRuleGroup CR holds a whole
    list of rules, so the natural dedup/naming unit is the (folder, rule_group)
    pair rather than a single rule uid.
    """
    return sanitize_k8s_name(f"{prefix}-{folder_title}-{rule_group}")


def contact_point_cr_name(name: str, prefix: str = "migrated") -> str:
    return sanitize_k8s_name(f"{prefix}-{name}")


def normalize_title(title: str) -> str:
    """Fold whitespace/case so 'Team Alerts' == 'team  alerts'."""
    return " ".join(title.strip().lower().split())
