"""Supply the contact point credentials a snapshot cannot contain.

Grafana redacts secure settings on every read, so a snapshot never holds the
real PagerDuty key or Slack webhook -- by design. The operator path works
around this by emitting a placeholder Secret for a human to fill in. Over HTTP
there is no secretKeyRef indirection, so the values have to come from
somewhere at import time; this is that somewhere.

Keys are contact point *names*, matched the same normalized way dedup matches
them, so "Critical PagerDuty" and "critical pagerduty" are the same receiver.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Iterable, Mapping

import yaml

from .models import SourceContactPoint
from .naming import normalize_title

logger = logging.getLogger(__name__)

SecretsMap = dict[str, dict[str, str]]


class SecretsFileError(RuntimeError):
    """Raised when a --secrets-file can't be read or isn't the expected shape."""


def load_secrets_file(path: Path) -> SecretsMap:
    """Load a YAML or JSON mapping of {contact point name: {field: value}}.

    YAML parses JSON too, so one loader covers both. Values are coerced to str
    so an integration key that looks like a number does not arrive as an int.
    """
    try:
        raw_text = path.read_text()
    except OSError as exc:
        raise SecretsFileError(f"cannot read secrets file {path}: {exc}") from exc

    try:
        parsed = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SecretsFileError(f"secrets file {path} is not valid YAML/JSON: {exc}") from exc

    if parsed is None:
        return {}
    if not isinstance(parsed, Mapping):
        raise SecretsFileError(
            f"secrets file {path} must be a mapping of contact point name -> field -> value, "
            f"got {type(parsed).__name__}"
        )

    out: SecretsMap = {}
    for cp_name, fields in parsed.items():
        if not isinstance(fields, Mapping):
            raise SecretsFileError(
                f"secrets file {path}: entry {cp_name!r} must be a mapping of field -> value, "
                f"got {type(fields).__name__}"
            )
        out[normalize_title(str(cp_name))] = {str(k): "" if v is None else str(v) for k, v in fields.items()}
    return out


def secrets_for(secrets: Mapping[str, Mapping[str, str]], contact_point_name: str) -> dict[str, str]:
    """The supplied field values for one contact point, or an empty mapping."""
    return dict(secrets.get(normalize_title(contact_point_name), {}))


def validate_secrets(
    secrets: Mapping[str, Mapping[str, str]],
    contact_points: Iterable[SourceContactPoint],
) -> list[str]:
    """Warn about entries that will have no effect.

    A typo in a contact point name or a field name is otherwise invisible: the
    import succeeds, and the integration is quietly dead. Cheap to check, and
    the most common way to get this file wrong.
    """
    warnings: list[str] = []
    known = {normalize_title(cp.name): cp for cp in contact_points}
    for key, fields in secrets.items():
        cp = known.get(key)
        if cp is None:
            warnings.append(
                f"secrets file has an entry for {key!r}, which is not a contact point being imported "
                "-- check the name, or it may already exist on the target"
            )
            continue
        for field_name in fields:
            if field_name not in cp.secure_field_names:
                warnings.append(
                    f"secrets file sets {field_name!r} on contact point {cp.name!r}, which has no such "
                    f"secure field (it has: {', '.join(cp.secure_field_names) or 'none'})"
                )
    return warnings


def secrets_skeleton(contact_points: Iterable[SourceContactPoint]) -> str:
    """A fill-in-the-blanks file covering exactly the redacted fields.

    Generated from the plan rather than guessed at, so it lists what this
    specific import will actually need and nothing else.
    """
    lines = [
        "# grafana-migrator secrets file",
        "#",
        "# Contact point secure fields, which Grafana redacts on read and so are",
        "# never present in a snapshot. Fill in the values and pass this file with",
        "# --secrets-file. Any field left empty is omitted from the request rather",
        "# than sent blank, which leaves that integration disabled until it is set.",
        "#",
        "# Keep this file out of version control.",
        "",
    ]
    needing: list[SourceContactPoint] = [cp for cp in contact_points if cp.secure_field_names]
    if not needing:
        lines.append("# No contact point in this import has redacted fields -- nothing to fill in.")
        lines.append("{}")
        return "\n".join(lines) + "\n"

    for cp in needing:
        lines.append(f"# {cp.type} contact point")
        # json.dumps gives a double-quoted scalar, which is valid YAML and
        # safe for names containing spaces, colons or leading symbols.
        lines.append(f"{json.dumps(cp.name)}:")
        for field_name in cp.secure_field_names:
            lines.append(f'  {field_name}: ""')
        lines.append("")
    return "\n".join(lines) + "\n"
