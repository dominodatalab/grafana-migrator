# Advanced usage

This covers everything beyond the [README](../README.md) quick start:
exactly what each step does, the full requirements list, manual manifest
review, multi-cluster imports, alternate auth, ingress path prefixes, and
the complete flag/report reference.

## Architecture

The work is split into three independent steps, each with its own command:

| Command | Talks to | Purpose |
| --- | --- | --- |
| `grafana-migrator export` | source Grafana instance only | Fetch raw content, write a snapshot to disk |
| `grafana-migrator import` | target cluster only | Dedup a snapshot against the target, write (optionally apply) CR manifests |
| `grafana-migrator apply` | target cluster only | Re-apply a manifest directory `import` already wrote |

**No step needs both the source and the target at once.** `export` never
touches `kubectl` or any cluster -- it can run before an operator-managed
target instance even exists. `import`/`apply` never touch the source
Grafana instance's HTTP API or credentials -- once a snapshot exists on
disk, everything after that is `kubectl`-only. This also means a single
`export` snapshot is a portable, reusable artifact: run `import` against
it as many times, or against as many different target clusters, as you
like, without going back to the source instance.

Works against any Grafana instance reachable over HTTP: a bare install, one
running behind an ingress path prefix, or one reached via
`kubectl port-forward`. Nothing here is tied to a particular hosting
platform.

### `export`: source instance -> raw snapshot

1. Calls `GET /api/search` on the source instance to list every folder and
   dashboard, then `GET /api/dashboards/uid/:uid` for each dashboard's full
   JSON.
2. Unless `--skip-alerts`: calls `GET /api/v1/provisioning/alert-rules` and
   `GET /api/v1/provisioning/contact-points`.
3. Unless `--skip-alerts` or `--skip-notification-policy`: calls
   `GET /api/v1/provisioning/policies` for the single root
   notification-routing tree.
4. Writes every raw response as-is under `--output-dir`: `search.json`,
   `dashboards/<uid>.json` (one file per dashboard), `alert-rules.json`,
   `contact-points.json`, `notification-policy.json`, and a `meta.json`
   summary. No transformation, no dedup, no Kubernetes concepts at all --
   this step doesn't know what a namespace or an `instanceSelector` is.

### `import`: snapshot + target cluster -> CR manifests

1. Reads the snapshot directory `export` wrote.
2. Reads the target namespace's existing `GrafanaDashboard`/`GrafanaFolder`/
   `GrafanaAlertRuleGroup`/`GrafanaContactPoint`/`GrafanaNotificationPolicy`
   CRs via `kubectl get ... -o json` (read-only).
3. For each source dashboard, decides one of:
   - **migrate** -- no existing CR has this uid or (normalized) title.
   - **skip (uid match)** -- an existing `GrafanaDashboard` CR's embedded
     JSON already has this exact uid. Strongest signal; file-provisioned
     dashboards generally keep the same uid when re-provisioned as a CR.
   - **skip (title match)** -- no uid match, but an existing CR has the same
     title under a different uid (e.g. the same dashboard re-provisioned
     with a regenerated uid). Weaker signal, so it's reported separately and
     skipped by default; pass `--include-title-duplicates` to migrate it
     anyway.
4. For each source folder, reuses an existing `GrafanaFolder` CR if its
   title matches (so e.g. a folder that already exists on the target isn't
   recreated); otherwise generates a new one.
5. Unless `--skip-alerts`: dedups alert rules by rule `uid` against every
   existing `GrafanaAlertRuleGroup` CR's `spec.rules[].uid`. New rules are
   grouped by (source folder, rule group) and written as one
   `GrafanaAlertRuleGroup` CR per group, in the same folder the dashboard
   step already resolved (`folderRef` reused when the folder already
   exists on target).
6. Unless `--skip-alerts`: dedups contact points by (normalized) name
   against existing `GrafanaContactPoint` CRs. Grafana's own untouched
   default receiver is always skipped. **Secure fields (webhook URLs, API
   tokens, passwords) are never available from the API** -- Grafana redacts
   them on every read -- so each one is wired to a `valuesFrom.secretKeyRef`
   pointing at a companion placeholder `Secret` manifest (`stringData` with
   empty values) that you must populate with the real credentials before
   applying.
7. Unless `--skip-alerts` or `--skip-notification-policy`: skips the
   notification policy if it's still Grafana's untouched factory default
   (single `empty` receiver, no nested routes), or if the target namespace
   already has *any* `GrafanaNotificationPolicy` CR -- that CR represents
   the **entire** routing tree for an instance, so this tool will never
   risk generating a competing one and silently clobbering existing
   routing. Otherwise writes one `GrafanaNotificationPolicy` CR with the
   source's route tree.
8. Writes one YAML manifest per resource under `--output-dir` (`dashboards/`,
   `folders/`, `alert-rules/`, `contact-points/`, `notification-policy/`),
   plus a `report.json` describing every decision made. Optionally applies
   them immediately (`--apply`).

CR/file names are derived deterministically from the **source Grafana**
identity (`migrated-<uid>` for dashboards, `migrated-<folder-title>-<rule-group>`
for alert rule groups, `migrated-<contact-point-name>`), so re-running
`import` against the same snapshot always produces the same name --
`kubectl apply` is a no-op update, not a second copy.

### `apply`: re-apply a manifest directory

Scans a directory `import` already wrote for subdirectories containing at
least one `.yaml` file and runs `kubectl apply -R` scoped to just those --
equivalent to `kubectl apply -R -f <dir>/dashboards -f <dir>/folders ...`
for every subdirectory that has manifests in it. Never redoes discovery or
dedup; never touches the source Grafana instance.

## Full requirements

### Runtime

- Python 3.12 or later.
- `requests>=2.31` and `pyyaml>=6.0` (installed automatically, see
  `pyproject.toml`).
- `kubectl` on `PATH` -- **only needed for `import` and `apply`**, not for
  `export`. This tool never talks to the Kubernetes API directly; every
  `import`/`apply` read and write goes through a `kubectl` subprocess, so
  anything `kubectl` can reach and authenticate against, this tool can too
  (a direct context, a `kubectl proxy`, a bastion-tunnelled kubeconfig,
  etc.).

Without installing via `pip install -e .`, `python3 -m grafana_migrator ...`
works identically from the repo root as long as `requests`/`pyyaml` are
already on your `PYTHONPATH`.

### For `export`: the source Grafana instance

Nothing here involves Kubernetes at all -- `export` is a pure HTTP client.

- Any Grafana version that serves `GET /api/search`,
  `GET /api/dashboards/uid/:uid`, and (unless `--skip-alerts`)
  `GET /api/v1/provisioning/{alert-rules,contact-points,policies}` --
  i.e. any Grafana with unified alerting (legacy dashboard-alerting-only
  instances have no provisioning API to read from; use `--skip-alerts`
  against one of those).
- Network reachability from wherever you run this tool to the source
  instance's HTTP(S) API -- its public URL, a URL behind an ingress path
  prefix (see [Ingress path prefix](#ingress-path-prefix) below), or
  `kubectl port-forward -n <namespace> svc/<grafana-service> <local-port>:80`
  if it isn't otherwise reachable.
- **Credentials, preferably a Grafana service account token**
  (`--source-token` / `$GRAFANA_SOURCE_TOKEN`) with at least the **Admin**
  organization role -- reading alert rules, contact points, and the
  notification policy tree via the provisioning API requires it; a lower
  role can silently 403 on just the alerting endpoints while dashboard
  search/fetch still works, which reads as a confusing partial failure. See
  [Grafana's service account docs](https://grafana.com/docs/grafana/latest/administration/service-accounts/#service-accounts)
  for how to create one. A token also isn't affected by the admin-password
  gotcha described in [docs/LIMITATIONS.md](LIMITATIONS.md), and doesn't
  require knowing the admin username. `--source-user`/`--source-password`
  (basic auth) is supported as a fallback -- see
  [Basic auth instead of a token](#basic-auth-instead-of-a-token) below.
- The credentials' **organization** matters: this tool fetches whichever
  org the token/user defaults to (Grafana service account tokens are
  permanently scoped to the org they were created in). A multi-org source
  instance needs one `export` run per org, with per-org credentials --
  there is no `--source-org`/org-switching flag.

### For `import`/`apply`: the target cluster

Nothing here involves the source Grafana instance at all -- `import`/`apply`
only need `kubectl` access, and `import` additionally needs to know which
target `Grafana` instance to stamp onto every generated CR.

- **grafana-operator must already be installed** on the target cluster,
  with its CRDs present: `grafanadashboards`, `grafanafolders`,
  `grafanaalertrulegroups`, `grafanacontactpoints`, and
  `grafananotificationpolicies` (all under the `grafana.integreatly.org`
  API group). This tool only ever consumes these CRDs -- it does not
  install the operator or its CRDs for you. If they're missing, `import`
  fails immediately with `kubectl get grafanadashboards ... failed: ...
  the server doesn't have a resource type "grafanadashboards"` -- that
  error is about the *target* cluster/context `--namespace`/current context
  point at, never about the source instance, which `import` never touches.
- **A `Grafana` custom resource must already exist** in the target
  namespace, with at least one label that you'll pass via
  `--instance-selector`. Run `kubectl get grafana -n <namespace>
  --show-labels` to see them (e.g. a Helm-installed instance commonly
  carries `dashboards: <release-name>`, `alerts: <release-name>`, etc.).
  Every generated CR's `spec.instanceSelector.matchLabels` is stamped from
  this flag verbatim -- get the label wrong and every manifest applies
  cleanly but nothing shows up in Grafana, since the operator has nothing
  to match it against.
- **`kubectl` identity needs RBAC in the target namespace to:**
  - `get`/`list` on `grafanadashboards`, `grafanafolders`,
    `grafanaalertrulegroups`, `grafanacontactpoints`, and
    `grafananotificationpolicies` -- required for every `import` run (even
    `--dry-run`), since dedup reads these before deciding what's new.
  - `create`/`update`/`patch` on the same five resource kinds, plus
    `secrets`, if you use `import --apply` or `grafana-migrator apply` --
    not required for a plain `import` (which only writes local files).
- Only a **single** `Grafana` CR should match `--instance-selector` in the
  target namespace. If more than one does, `kubectl` and the operator will
  both happily proceed, but which instance actually receives the migrated
  content becomes ambiguous -- pick labels specific enough to select the
  one you mean.

## Reviewing manifests by hand before applying

Instead of `import --apply`, drop `--apply` (and `--dry-run`) to write
manifests without applying them:

```bash
grafana-migrator import ./grafana-migrator-source \
  --namespace domino-platform \
  --instance-selector dashboards=<label-value> \
  --output-dir ./grafana-migrator-manifests
```

This writes manifests to
`./grafana-migrator-manifests/{folders,dashboards,alert-rules,contact-points,notification-policy}/*.yaml`
(only the subdirectories that actually have new content -- re-running
against an already-fully-migrated target produces no files at all beyond
`report.json`), plus `report.json` describing every decision made. Every
manifest is plain, readable YAML -- multi-line content like a dashboard's
embedded JSON is written as a proper indented block, not an escaped
one-line string, so the files are meant to be opened and reviewed.

**If any `contact-points/*-secrets.yaml` files were written, open them and
fill in the real credentials before applying** -- they ship with empty
`stringData` values since Grafana never returns secret values via its API.
`report.json`'s `contact_points_migrated` list names exactly which secret
goes with which contact point.

Once you're happy with what's generated (and any secrets are populated),
apply it:

```bash
grafana-migrator apply ./grafana-migrator-manifests
```

## Basic auth instead of a token

```bash
export GRAFANA_SOURCE_USERNAME=admin
export GRAFANA_SOURCE_PASSWORD='...'   # prefer env vars over --source-password

grafana-migrator export \
  --source-url http://localhost:3000 \
  --output-dir ./grafana-migrator-source
```

Note: **the admin username is not necessarily `admin`** -- check the
deployment's `GF_SECURITY_ADMIN_USER` setting or the pod's startup logs.
See also the admin-password gotcha in
[docs/LIMITATIONS.md](LIMITATIONS.md#authentication).

## Ingress path prefix

If the source instance sits behind an ingress path prefix rather than at
the domain root (e.g. `https://cluster.example.com/grafana`), pass
`--source-path-segment grafana` and give `--source-url` as just the bare
host -- the prefix is appended automatically if it's missing. Leave
`--source-path-segment` unset and `--source-url` is used exactly as given.
This only applies to real hostnames; `localhost`/`127.0.0.1` URLs (i.e.
`kubectl port-forward` targets) are always left untouched, since a
port-forward hits the Service's root path directly with no ingress prefix
to add.

## Capturing a snapshot before a target exists

Because `export` never touches `kubectl` or a target cluster, it's safe to
run at any point in a migration timeline -- including well before an
operator-managed instance has been stood up. A common sequence:

```bash
# today, while only the legacy/standalone instance exists:
grafana-migrator export --source-url https://legacy.example.com \
  --output-dir ./grafana-migrator-source

# ... stand up the operator-managed instance whenever that happens ...

# once it exists:
grafana-migrator import ./grafana-migrator-source \
  --namespace domino-platform --instance-selector dashboards=<label-value> \
  --output-dir ./grafana-migrator-manifests --apply
```

## Importing one snapshot into several target clusters

Because `export`, `import`, and `apply` are all separate steps, both a
snapshot directory and a manifest directory are portable, self-contained
artifacts. A single `export` snapshot can be `import`-ed against more than
one target cluster -- useful for a fleet of otherwise-identical
operator-managed instances, or for staging the same content into a test
cluster before a production one:

```bash
grafana-migrator export --source-url ... --output-dir ./snapshot

grafana-migrator import ./snapshot --namespace domino-platform \
  --instance-selector dashboards=<label-value> \
  --output-dir ./manifests-a --kube-context cluster-a --apply
grafana-migrator import ./snapshot --namespace domino-platform \
  --instance-selector dashboards=<label-value> \
  --output-dir ./manifests-b --kube-context cluster-b --apply
```

Each `import` dedups independently against *that* target's own existing
content, so this is safe even if cluster-a and cluster-b are in different
migration states. If you instead already have a manifest directory from a
previous `import` and just want to re-apply the exact same manifests
(skipping dedup) to more than one cluster:

```bash
grafana-migrator apply ./manifests-a --kube-context cluster-a
grafana-migrator apply ./manifests-a --kube-context cluster-b
```

Each `apply` re-runs the same `kubectl apply -R`, so it's idempotent per
target -- applying the same directory twice against the same cluster is a
no-op the second time. `apply` never re-queries any cluster's existing CRs,
so reusing one manifest directory across two targets only makes sense if
you know both are in the same pre-migration state; when in doubt, run
`import` separately against each so dedup reflects that target's actual
content.

## Commands

| Command | Purpose |
| --- | --- |
| `grafana-migrator export ...` | Fetch from a source Grafana instance, write a raw snapshot. No target cluster involved. |
| `grafana-migrator import <snapshot-dir> ...` | Dedup a snapshot against a target cluster, write (optionally apply) CR manifests + report |
| `grafana-migrator apply <manifest-dir>` | Re-apply a manifest directory `import` already wrote, without redoing discovery or dedup |

A bare `grafana-migrator` invocation with no subcommand (or an unrecognized
one) prints usage and exits 2 -- there is no default subcommand, since
`export` and `import` need very different flags.

## `export` flag reference

| Flag | Env var | Default | Purpose |
| --- | --- | --- | --- |
| `--source-url` | -- | *(required)* | Base URL of the source Grafana instance |
| `--source-path-segment` | `GRAFANA_SOURCE_PATH_SEGMENT` | unset | Ingress path prefix to append to `--source-url` (skipped for localhost URLs) |
| `--source-token` | `GRAFANA_SOURCE_TOKEN` | unset | Service account token; preferred auth method, takes precedence over user/password if both given |
| `--source-user` | `GRAFANA_SOURCE_USERNAME` | unset | Basic-auth username, used only if `--source-token` isn't set |
| `--source-password` | `GRAFANA_SOURCE_PASSWORD` | unset | Basic-auth password; prefer the env var so it doesn't land in shell history/process list |
| `--output-dir` | -- | `./grafana-migrator-source` | Directory to write the raw snapshot into |
| `--skip-alerts` | -- | off | Don't fetch alert rules or contact points (dashboards/folders only) |
| `--skip-notification-policy` | -- | off | Don't fetch the notification policy tree |
| `--dry-run` | -- | off | Fetch and print counts; don't write the snapshot to disk |
| `-v`, `--verbose` | -- | off | Debug logging, including every HTTP request/response |

At least one of `--source-token` or (`--source-user` and
`--source-password`) is required; `export` exits 2 with an error message
if neither is given.

## `import` flag reference

| Flag | Default | Purpose |
| --- | --- | --- |
| `export_dir` (positional) | *(required)* | Directory previously written by `grafana-migrator export --output-dir ...` |
| `--namespace` | *(required)* | Target namespace holding the operator-managed `Grafana` instance |
| `--kube-context` | current `kubectl` context | Context to read from/write to on the target cluster |
| `--instance-selector` | *(required)* | Comma-separated `key=value` labels stamped as `instanceSelector.matchLabels` on every generated CR; must match a label actually present on the target `Grafana` CR |
| `--output-dir` | `./grafana-migrator-import` | Directory to write generated manifests + `report.json` into |
| `--include-title-duplicates` | off | Migrate title-collision dashboards instead of skipping them |
| `--skip-alerts` | off | Don't import alert rules or contact points, even if the snapshot has them |
| `--skip-notification-policy` | off | Don't import the notification policy tree, even if the snapshot has a custom one |
| `--dry-run` | off | Discover + dedup + print the report; write nothing (mutually exclusive with `--apply`) |
| `--apply` | off | Write manifests, then immediately run the equivalent of `grafana-migrator apply <output-dir>` (mutually exclusive with `--dry-run`) |
| `--report-format` | `text` | `text` or `json` |
| `-v`, `--verbose` | off | Debug logging, including every `kubectl` invocation |

If the snapshot was captured with `--skip-alerts`/`--skip-notification-policy`
at export time, `import` treats those categories as absent regardless of
its own flags -- there's nothing to import that was never fetched.

## `apply` flag reference

| Flag | Default | Purpose |
| --- | --- | --- |
| `export_dir` (positional) | *(required)* | Directory previously written by `grafana-migrator import --output-dir ...` |
| `--kube-context` | current `kubectl` context | Context to apply the manifests to |
| `-v`, `--verbose` | off | Debug logging |

## Snapshot directory contents (`export`'s output)

`search.json` (raw `/api/search` response), `dashboards/<uid>.json` (one
raw `/api/dashboards/uid/:uid` response per dashboard), `alert-rules.json`,
`contact-points.json`, `notification-policy.json` (each absent if
`--skip-alerts`/`--skip-notification-policy` was passed at export time),
and `meta.json` (a small summary: dashboard count, and whether alerts/the
notification policy were fetched at all). These are Grafana's own raw API
responses, not yet transformed into anything Kubernetes-shaped -- `import`
does all of that. Safe to inspect directly if you want to see exactly
what the source instance returned before any transformation happens.

## `report.json` fields (`import`'s output)

The report (also printed as text unless `--report-format json`) records
every decision made, keyed by resource type:
`migrated` / `skipped_uid_match` / `skipped_title_match` for dashboards,
`folders_created` / `folders_reused`,
`alert_rules_migrated` / `alert_rules_skipped_uid_match`,
`contact_points_migrated` (each entry names its companion secret, if any)
/ `contact_points_skipped_name_match` / `contact_points_skipped_default`,
and `notification_policy_status` / `notification_policy_detail` (one of
`migrated`, `skipped_default`, `skipped_target_has_policy`,
`skipped_unavailable`, or `skipped_by_flag`, with `_detail` explaining
which and why). Check this file (or the equivalent text report) whenever
migrated/skipped counts look surprising -- every limitation in
[docs/LIMITATIONS.md](LIMITATIONS.md) that affects dedup behavior shows up
here first.
