# Known limitations

See the [README](../README.md) for the quick start and
[docs/ADVANCED.md](ADVANCED.md) for the full command/flag reference.

## Not yet implemented

- **Data sources are not yet handled.**

## Dedup and import behavior

- **Alert rule dedup is uid-only**, with no title fallback (unlike
  dashboards). Observed rule uids are deterministic human-derived ids (e.g.
  `PVUsageCritical_id`) rather than random strings, so this hasn't been a
  problem in practice -- revisit if a source instance shows otherwise.
- **Title-match dedup (dashboards only) is a heuristic**, not a guarantee:
  two unrelated dashboards that happen to share a title will be (correctly,
  conservatively) flagged and skipped, not silently merged. Check
  `report.json`'s `skipped_title_match` list if the counts look off.
- **Two source folders with the same title collapse into one target
  folder.** Grafana only enforces folder uid uniqueness, not title
  uniqueness, but folder dedup and CR naming are both title-based (see
  `FolderIndex`/`folder_cr_name`). If a source instance has two distinct
  folders sharing a title, dashboards from both are silently pointed at the
  same generated `GrafanaFolder` CR -- check for duplicate titles in the
  source's folder list before migrating if the folders carry different
  permissions.
- **Nested folders are flattened.** The `GrafanaFolder` CRD supports
  `parentFolderRef`/`parentFolderUID`, but this tool doesn't read or set
  either -- every migrated folder lands at the top level of the target
  instance regardless of its source nesting. Applies cleanly, just loses
  the parent/child structure; re-nest by hand afterward if it matters.
- **`GET /api/search` is called with a fixed `limit=5000` and no
  pagination.** A source instance with more than 5000 combined
  dashboards+folders will have the excess silently missing from the
  snapshot -- there's no warning, since Grafana's API doesn't distinguish
  "exactly 5000 results" from "more exist, truncated at 5000." Unlikely in
  practice, but worth knowing if a source instance is unusually large.
- **Only the source credentials' own Grafana organization is fetched.**
  A multi-org source instance needs one `export` run per org with
  per-org credentials; there's no flag to select or iterate orgs.

## Contact points and notification policy

- **Contact point secure fields are never migrated with real values** --
  Grafana's provisioning API redacts them on every read, by design. The
  generated `GrafanaContactPoint` CR references a placeholder `Secret` that
  must be populated by hand before the integration will actually work.
- **Notification policy import is intentionally conservative**: it's
  skipped entirely if the target namespace already has any
  `GrafanaNotificationPolicy` CR, since that CR replaces the whole routing
  tree and this tool has no tree-merge logic. Check `report.json`'s
  `notification_policy_status` for why it was or wasn't imported.
- **Notification policy matchers are written as `object_matchers` triples,
  not the CRD's structured `matchers` objects.** Both fields exist on the
  `GrafanaNotificationPolicy`/`GrafanaNotificationPolicyRoute` CRDs, but on
  grafana-operator 5.24.0 the structured `{name, value, isEqual, isRegex}`
  form fails to translate into a request Grafana's own `PUT
  /api/v1/provisioning/policies` API accepts -- confirmed live: a
  schema-valid CR with structured matchers still gets rejected with a
  generic `400 putPolicyTreeBadRequest` from Grafana, and
  `NotificationPolicySynchronized` shows `ApplyFailed`. The `object_matchers`
  field (`[][]string` triples, e.g. `[["team", "=", "x"]]`) round-trips
  correctly on the same operator version -- confirmed live end-to-end,
  including a real per-team/per-severity nested route. If a future
  operator release changes how it maps one or the other field, check
  `kubectl describe grafananotificationpolicy <name>` for
  `NotificationPolicySynchronized: ApplyFailed` as a sign this may need
  revisiting.

## Alert rules

- **Alert recording rules are not supported and will fail to apply.**
  Grafana's provisioning API returns `condition`/`noDataState`/`execErrState`
  as empty strings for a `record`-type rule (they're meaningless for a rule
  that doesn't fire), but this tool passes them through unchanged. The
  `GrafanaAlertRuleGroup` CRD requires non-empty enum values for all three,
  so `kubectl apply` rejects the whole rule group outright with
  `Unsupported value: ""`. Recording rules need to be recreated by hand on
  the target; regular alerting rules in the same source folder/rule group
  are unaffected since each group is its own CR.

## Cluster/instance targeting

- Assumes a single Grafana instance per target namespace matches
  `--instance-selector`; if there are multiple, pick labels specific enough
  to select the one you mean.
- CR/Secret names are truncated to Kubernetes' 253-character DNS-1123
  subdomain limit if a derived name (e.g. `migrated-<very long title>`)
  would exceed it. Two source items whose names only differ past that
  truncation point would collide -- pathological in practice, since it
  requires 253+ character titles that are also otherwise identical.

## Authentication

- If using basic auth, the source instance's admin password may not
  authenticate even with the correct credentials from its k8s Secret:
  Grafana only applies `GF_SECURITY_ADMIN_PASSWORD` when the admin user is
  created on first boot, not on later restarts against a pre-existing
  `grafana.db` (e.g. a restored PV). If `--source-user`/`--source-password`
  401 despite matching the Secret, this is likely why -- a password reset
  (`grafana cli admin reset-admin-password` inside the pod) is needed, not
  a different credential. **Use `--source-token` instead to avoid this
  class of problem entirely.**
