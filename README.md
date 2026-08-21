# grafana-migrator

Migrates dashboards, folders, alert rules, contact points, and the
notification policy from a **standalone Grafana instance** to **Grafana
Operator** custom resources on a target cluster. Safe to re-run: anything
that's already on the target is skipped, not duplicated.

The work happens in two steps: `export` talks only to the source Grafana
instance over HTTP; `import` talks only to the target cluster via `kubectl`,
reading the snapshot `export` wrote. Neither step needs the other's
credentials or reachability at the same time.

## Prerequisites

- Python 3.12+.
- `kubectl` on `PATH`, pointed at the target cluster, with **grafana-operator
  already installed** there and a `Grafana` custom resource already present
  in the namespace you're migrating into.
- A Grafana **service account token** from the source instance. Follow
  [Grafana's service account docs](https://grafana.com/docs/grafana/latest/administration/service-accounts/#service-accounts)
  to create one, and give it the **Admin** organization role -- a lower
  role can silently fail to read alert rules/contact points/the
  notification policy while dashboards still work, which reads as a
  confusing partial migration.

Data sources are not yet handled by this tool. See
[docs/LIMITATIONS.md](docs/LIMITATIONS.md) for this and other known gaps.

## Install

Use a virtual environment (standard practice for any Python CLI, not a
workaround specific to this tool):

```bash
git clone https://github.com/dominodatalab/grafana-migrator.git
cd grafana-migrator
python3 -m venv .venv
source .venv/bin/activate
pip install -e .
```

This registers the `grafana-migrator` command for as long as the venv is
active. Run `grafana-migrator --help` to confirm it worked.

## Development

```bash
pip install -e '.[dev]'   # adds black, ruff, mypy, pytest
pytest -q
ruff check src tests
black --check src tests
mypy
```

`black` is pinned to an exact version on purpose -- its stable style shifts
between majors, so an unpinned formatter turns every upgrade into a
reformat-the-world diff. All four checks run in CI against Python 3.12 (the
declared floor) and 3.13.

## Quick start

```bash
# 1. Capture everything from the source (legacy) instance -- no target
#    cluster involved yet
export GRAFANA_SOURCE_TOKEN='glsa_...'
grafana-migrator export \
  --source-url https://grafana.example.com/grafana-classic \
  --output-dir ./grafana-migrator-source

# 2. Dry-run against the target namespace to see what's genuinely new
grafana-migrator import ./grafana-migrator-source \
  --namespace domino-platform \
  --instance-selector dashboards=grafana-platform \
  --output-dir ./grafana-migrator-manifests \
  --dry-run

# 3. Once the report looks right, write the manifests and apply them
grafana-migrator import ./grafana-migrator-source \
  --namespace domino-platform \
  --instance-selector dashboards=grafana-platform \
  --output-dir ./grafana-migrator-manifests \
  --apply
```

`GRAFANA_SOURCE_TOKEN` is scoped to whichever source instance it was issued
by -- if you're migrating from more than one source instance, re-export it
with that instance's own token before running `export` again.

That's it for most migrations. Afterward:

- Check `./grafana-migrator-manifests/report.json` for what was migrated vs.
  skipped and why.
- **If any `contact-points/*-secrets.yaml` files were written, open them and
  fill in the real credentials** — Grafana never returns secret values
  (webhook URLs, API tokens, passwords) via its API, so those manifests ship
  with empty placeholders that won't work until you populate them.

## Advanced usage

The quick start above covers the common path. For everything else —
reviewing generated manifests by hand before applying, applying to multiple
target clusters, basic auth instead of a token, ingress path prefixes, the
full command/flag reference, and exactly what each step does under the
hood — see [docs/ADVANCED.md](docs/ADVANCED.md).

For known limitations (unsupported alert rule types, dedup edge cases,
notification policy caveats, etc.), see
[docs/LIMITATIONS.md](docs/LIMITATIONS.md).
