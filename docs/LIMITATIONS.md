# Known limitations

See the [README](../README.md) for the quick start and
[docs/ADVANCED.md](ADVANCED.md) for the full command/flag reference.

Unless a bullet says otherwise, it applies to both `--target operator` and
`--target api`. See [Choosing a backend](ADVANCED.md#choosing-a-backend)
for how the two differ operationally.

## Not yet implemented

- **Data sources are not yet handled**, by either backend. A dashboard's
  panels still reference whatever datasource uid the source Grafana used;
  neither backend remaps or recreates it, so a panel querying a
  source-only datasource stays broken on the target until a datasource
  with a matching uid exists there.

## Dedup and import behavior

- **Alert rule dedup is uid-only**, with no title fallback (unlike
  dashboards). Observed rule uids are deterministic human-derived ids (e.g.
  `PVUsageCritical_id`) rather than random strings, so this hasn't been a
  problem in practice -- revisit if a source instance shows otherwise.
- **Title-match dedup (dashboards only) is a heuristic**, not a guarantee:
  two unrelated dashboards that happen to share a title will be (correctly,
  conservatively) flagged and skipped, not silently merged. Check
  `report.json`'s `skipped_title_match` list if the counts look off.
  **This fires far more often with `--target api`:** the target's
  `/api/search` returns *every* dashboard on it, not just previously
  migrated ones, so any pre-existing, unrelated dashboard that happens to
  share a title trips this. `--include-title-duplicates` is commonly
  needed with this backend.
- **Two source folders with the same title collapse into one target
  folder (`--target operator` only).** Grafana only enforces folder uid
  uniqueness, not title uniqueness, but folder dedup and CR naming are both
  title-based (see `FolderIndex`/`folder_cr_name`). If a source instance
  has two distinct folders sharing a title, dashboards from both are
  silently pointed at the same generated `GrafanaFolder` CR -- check for
  duplicate titles in the source's folder list before migrating if the
  folders carry different permissions.
  **`--target api` doesn't collapse them, but mishandles them
  differently:** each source folder is created by its own preserved uid,
  so Grafana's own title-uniqueness-within-a-parent constraint rejects the
  second one with a `409`. That `409` is indistinguishable from "this exact
  folder is already on the target from a previous run" and is recorded the
  same way -- as a skip, not a failure -- and the source folder's own uid
  is then assumed to be the target folder's uid for every dashboard/rule
  that references it. Since that uid was never actually created on the
  target, those dependent writes are likely to fail in turn. Unverified
  live; check for duplicate source folder titles before migrating in api
  mode too, for a different reason than operator mode.
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
  **`--target api` calls the same endpoint against the target** to build
  its dedup inventory, so an unusually large *target* is silently
  under-read the same way -- objects past the 5000th are invisible to
  dedup, which can produce a false "this is new" decision. The
  409-is-a-skip handling (see
  [Ordering and partial failure](ADVANCED.md#ordering-and-partial-failure-target-api-only))
  is what keeps that from duplicating anything: worst case is a wasted
  write attempt that Grafana itself rejects, not a real duplicate.
- **Only the source credentials' own Grafana organization is fetched.**
  A multi-org source instance needs one `export` run per org with
  per-org credentials; there's no flag to select or iterate orgs.

## Contact points and notification policy

- **Contact point secure fields are never migrated with real values** --
  Grafana's provisioning API redacts them on every read, by design. In
  `--target operator` mode the generated `GrafanaContactPoint` CR
  references a placeholder `Secret` that must be populated by hand (or via
  `--secrets-file`) before the integration will actually work. **In
  `--target api` mode there is no k8s Secret at all** -- `--secrets-file`
  is the only way to supply real values; anything not in that file is
  created with the field simply absent, not empty, and stays disabled
  until re-supplied on a later run. See
  [Supplying contact point secrets](ADVANCED.md#supplying-contact-point-secrets-with---secrets-file).
- **Notification policy import is intentionally conservative**: it's
  skipped entirely if the target already has a non-default notification
  policy, since importing one replaces the *entire* routing tree and this
  tool has no tree-merge logic. In `--target operator` mode that guardrail
  is "does the target namespace already have any
  `GrafanaNotificationPolicy` CR"; in `--target api` mode it's "is the
  target's current tree still Grafana's factory default, and does it carry
  no `provenance`" -- a provisioned (file- or operator-managed) tree on the
  target is skipped too, since a `PUT` would fight whatever else manages
  it. Check `report.json`'s `notification_policy_status` for why it was or
  wasn't imported. **There is no rollback**: the `PUT` in `--target api`
  mode replaces the whole tree in one request with no prior version kept
  by this tool.
- **Notification policy matchers are written as `object_matchers` triples,
  not the CRD's structured `matchers` objects (`--target operator` only).**
  Both fields exist on the
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
  revisiting. **`--target api` mode has no equivalent problem** -- it PUTs
  the policy tree it read back verbatim (minus `provenance`) to the same
  kind of endpoint it came from, so matchers round-trip by construction;
  this is unverified live, though, like the rest of the api backend.
- **Migrated objects are provisioned (read-only in the target's UI) by
  default in `--target api` mode**, signalling that the snapshot -- not
  the target's UI -- is the source of truth. Pass `--editable` to send
  `X-Disable-Provenance` and leave them editable instead. `--target
  operator` mode's editability follows whatever the operator itself sets;
  this tool has no equivalent flag there.

## Alert rules

- **Alert recording rules are not supported and will fail to apply
  (`--target operator` only).** Grafana's provisioning API returns
  `condition`/`noDataState`/`execErrState` as empty strings for a
  `record`-type rule (they're meaningless for a rule that doesn't fire),
  but this tool passes them through unchanged. The `GrafanaAlertRuleGroup`
  CRD requires non-empty enum values for all three, so `kubectl apply`
  rejects the whole rule group outright with `Unsupported value: ""`.
  Recording rules need to be recreated by hand on the target; regular
  alerting rules in the same source folder/rule group are unaffected since
  each group is its own CR. **`--target api` mode omits those three fields
  entirely for a `record`-type rule** rather than sending them empty, since
  Grafana's provisioning API rejects an empty string the same way the CRD
  does. Not yet confirmed against a live recording rule, though -- treat as
  likely fixed, not confirmed fixed, until it is.
- **Alert rule group `interval` is not captured by `export` at all**,
  regardless of what the source group actually used. The two backends cope
  differently: `--target operator` hardcodes `interval: "1m"` on every
  generated `GrafanaAlertRuleGroup` CR. `--target api` doesn't set an
  interval at all -- `GrafanaClient.set_alert_rule_group_interval()` exists
  but isn't called from the push path yet, so a newly-created group is left
  at whatever Grafana itself defaults a group to. Backend-neutral root
  cause; the real fix is `export` additionally reading each group's
  interval from
  `GET /api/v1/provisioning/alert-rules/groups/:folderUID/:group`.

## Cluster/instance targeting (`--target operator` only)

- Assumes a single Grafana instance per target namespace matches
  `--instance-selector`; if there are multiple, pick labels specific enough
  to select the one you mean.
- CR/Secret names are truncated to Kubernetes' 253-character DNS-1123
  subdomain limit if a derived name (e.g. `migrated-<very long title>`)
  would exceed it. Two source items whose names only differ past that
  truncation point would collide -- pathological in practice, since it
  requires 253+ character titles that are also otherwise identical.

`--target api` has no equivalent section: there's no CR, no namespace, no
`--instance-selector` -- identity on the target is just whatever uid/name
Grafana itself assigns or is asked to preserve.

## Authentication

- If using basic auth, a Grafana instance's admin password may not
  authenticate even with the correct credentials from its k8s Secret:
  Grafana only applies `GF_SECURITY_ADMIN_PASSWORD` when the admin user is
  created on first boot, not on later restarts against a pre-existing
  `grafana.db` (e.g. a restored PV). If `--source-user`/`--source-password`
  (or, for `--target api`, `--dest-user`/`--dest-password`) 401 despite
  matching the Secret, this is likely why -- a password reset
  (`grafana cli admin reset-admin-password` inside the pod) is needed, not
  a different credential. **Use a service account token
  (`--source-token`/`--dest-token`) instead to avoid this class of problem
  entirely.**
