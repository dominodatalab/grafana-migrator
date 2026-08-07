import yaml

from grafana_migrator.yaml_output import dump_manifest


def test_multiline_string_renders_as_literal_block_not_escaped_scalar():
    manifest = {"spec": {"json": '{\n  "uid": "x"\n}\n'}}
    rendered = dump_manifest(manifest)
    assert "|" in rendered
    assert "\\n" not in rendered


def test_single_line_string_is_unaffected():
    manifest = {"metadata": {"name": "migrated-x"}}
    rendered = dump_manifest(manifest)
    assert "name: migrated-x" in rendered


def test_rendered_yaml_round_trips_to_the_same_manifest():
    manifest = {
        "apiVersion": "grafana.integreatly.org/v1beta1",
        "kind": "GrafanaDashboard",
        "spec": {"json": '{\n  "uid": "test123",\n  "panels": []\n}\n', "folder": "General"},
    }
    rendered = dump_manifest(manifest)
    assert yaml.safe_load(rendered) == manifest
