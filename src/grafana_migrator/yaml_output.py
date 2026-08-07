"""YAML rendering for generated manifests.

Forces literal block style (`|`) for any multi-line string, so embedded
dashboard JSON reads as indented JSON rather than PyYAML's default escaped
`\\n` wall of text.
"""

from __future__ import annotations

from typing import Any

import yaml


class _ManifestDumper(yaml.SafeDumper):
    pass


def _represent_str(dumper: yaml.SafeDumper, data: str) -> yaml.ScalarNode:
    style = "|" if "\n" in data else None
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=style)


_ManifestDumper.add_representer(str, _represent_str)


def dump_manifest(manifest: dict[str, Any]) -> str:
    """Render one manifest as human-readable YAML, ready to review and apply."""
    return yaml.dump(manifest, Dumper=_ManifestDumper, sort_keys=False, default_flow_style=False)
