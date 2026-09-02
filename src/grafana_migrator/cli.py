"""CLI entrypoints:

- `grafana-migrator export` talks only to the source Grafana instance's HTTP
  API and writes its raw responses to disk. No target cluster is involved --
  this can run before an operator-managed target instance even exists.
- `grafana-migrator import` reads a directory `export` wrote, dedups it
  against whatever the target already has, and writes whatever is genuinely
  new. `--target operator` (the default) dedups against the target cluster's
  GrafanaDashboard/GrafanaFolder/... CRs and writes -- optionally applies --
  CR manifests. `--target api` dedups against a target Grafana's own HTTP API
  and pushes straight to it, needing no operator and no kubectl. Either way
  there is no further dependency on the source instance.
- `grafana-migrator apply <dir>` applies a manifest directory `import`
  already wrote, without redoing discovery or dedup.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

from .api_backend import ApiPushOptions, push
from .grafana_client import GrafanaClientError, build_client
from .grafana_inventory import build_target_inventory as build_grafana_inventory
from .import_plan import ImportPlan, IncompleteSnapshotError, PlanOptions, plan_import
from .k8s_inventory import KubectlError
from .k8s_inventory import build_target_inventory as build_kubectl_inventory
from .operator_backend import emit_manifests
from .report import MigrationReport
from .secrets_file import SecretsFileError, load_secrets_file, secrets_skeleton, validate_secrets
from .source_dump import SourceDumpError, fetch_source, read_source_dump, write_source_dump
from .yaml_output import dump_manifest

logger = logging.getLogger("grafana_migrator")


def parse_selector(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue
        if "=" not in pair:
            raise argparse.ArgumentTypeError(f"invalid selector segment {pair!r}, expected key=value")
        k, v = pair.split("=", 1)
        result[k.strip()] = v.strip()
    if not result:
        raise argparse.ArgumentTypeError("--instance-selector must contain at least one key=value pair")
    return result


def _manifest_subdirs(manifests: list[tuple[str, dict[str, Any]]]) -> list[str]:
    """Top-level subdirectory names actually written under --output-dir.

    Scopes `kubectl apply` to the manifest subdirs, excluding report.json.
    """
    return sorted({rel_path.split("/", 1)[0] for rel_path, _ in manifests})


# ---------------------------------------------------------------------------
# export: source instance -> raw snapshot on disk. No target cluster.
# ---------------------------------------------------------------------------


def build_export_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grafana-migrator export",
        description=(
            "Fetch dashboards, folders, alert rules, contact points, and the notification "
            "policy from a source Grafana instance and write the raw API responses to "
            "--output-dir. Talks only to the source instance -- no kubectl, no target "
            "cluster, no --namespace/--instance-selector needed. Run `grafana-migrator "
            "import <output-dir> ...` afterward to dedup this snapshot against a target "
            "cluster and generate CR manifests."
        ),
    )
    p.add_argument(
        "--source-url", required=True, help="Base URL of the source Grafana instance, e.g. http://localhost:3000"
    )
    p.add_argument(
        "--source-path-segment",
        default=os.environ.get("GRAFANA_SOURCE_PATH_SEGMENT"),
        help="If the source instance sits behind an ingress path prefix (e.g. 'grafana' for "
        "https://host.example.com/grafana), pass it here and --source-url can be given as just "
        "the bare host -- the prefix is appended if not already present. Left unset, --source-url "
        "is used exactly as given. Has no effect on localhost/127.0.0.1 URLs (kubectl port-forward "
        "targets, which have no ingress prefix).",
    )
    p.add_argument(
        "--source-token",
        default=os.environ.get("GRAFANA_SOURCE_TOKEN"),
        help="Source Grafana service account token (default: $GRAFANA_SOURCE_TOKEN). Preferred over "
        "--source-user/--source-password: a token isn't affected by the admin user's password ever "
        "having drifted from GF_SECURITY_ADMIN_PASSWORD (Grafana only applies that env var when the "
        "admin user is first created, not on later restarts), and doesn't require knowing the admin "
        "username. Takes precedence if both a token and user/password are given.",
    )
    p.add_argument(
        "--source-user",
        default=os.environ.get("GRAFANA_SOURCE_USERNAME"),
        help="Source Grafana admin username (default: $GRAFANA_SOURCE_USERNAME), used if --source-token "
        "is not given. Note: may not be 'admin' -- check the deployment's GF_SECURITY_ADMIN_USER.",
    )
    p.add_argument(
        "--source-password",
        default=os.environ.get("GRAFANA_SOURCE_PASSWORD"),
        help="Source Grafana admin password (default: $GRAFANA_SOURCE_PASSWORD; prefer the "
        "env var over this flag so the secret doesn't show up in shell history/process list)",
    )
    p.add_argument(
        "--output-dir",
        default="./grafana-migrator-source",
        help="Directory to write the raw source snapshot into (search.json, dashboards/*.json, "
        "alert-rules.json, contact-points.json, notification-policy.json, meta.json)",
    )
    p.add_argument(
        "--skip-alerts",
        action="store_true",
        help="Don't fetch alert rules or contact points (dashboards/folders only)",
    )
    p.add_argument(
        "--skip-notification-policy",
        action="store_true",
        help="Don't fetch the notification policy tree",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print counts, but don't write the snapshot to disk",
    )
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def run_export(argv: list[str] | None = None) -> int:
    args = build_export_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    if not args.source_token and not (args.source_user and args.source_password):
        print(
            "error: source credentials required (--source-token / $GRAFANA_SOURCE_TOKEN, or "
            "--source-user/--source-password / $GRAFANA_SOURCE_USERNAME/$GRAFANA_SOURCE_PASSWORD)",
            file=sys.stderr,
        )
        return 2

    try:
        client = build_client(
            url=args.source_url,
            token=args.source_token,
            user=args.source_user,
            password=args.source_password,
            path_segment=args.source_path_segment,
            flag_prefix="source",
        )
        dump = fetch_source(
            client, skip_alerts=args.skip_alerts, skip_notification_policy=args.skip_notification_policy
        )
    except GrafanaClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    dashboard_count = len(dump.dashboards_by_uid)
    folder_count = sum(1 for i in dump.search_results if i.get("type") == "dash-folder")
    print(
        f"fetched {dashboard_count} dashboard(s), {folder_count} folder(s) from {args.source_url}\n"
        f"  alert rules: {len(dump.alert_rules_raw) if dump.alert_rules_raw is not None else 'not fetched'}\n"
        f"  contact points: {len(dump.contact_points_raw) if dump.contact_points_raw is not None else 'not fetched'}\n"
        f"  notification policy: {'fetched' if dump.notification_policy_raw is not None else 'not fetched'}"
    )

    if args.dry_run:
        return 0

    out_dir = Path(args.output_dir)
    write_source_dump(dump, out_dir)
    logger.info("wrote source snapshot to %s", out_dir)
    return 0


# ---------------------------------------------------------------------------
# import: raw snapshot + target cluster -> dedup + CR manifests (+ apply)
# ---------------------------------------------------------------------------


def build_import_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grafana-migrator import",
        description=(
            "Dedup a source snapshot written by `grafana-migrator export` against whatever the "
            "target already has, and write whatever is genuinely new. --target operator (the "
            "default) dedups against the target cluster's GrafanaDashboard/GrafanaFolder/... CRs "
            "and writes (optionally applies) CR manifests. --target api dedups against a target "
            "Grafana's own HTTP API and pushes straight to it. Either way, never talks to the "
            "source Grafana instance."
        ),
    )
    p.add_argument("export_dir", help="Directory previously written by `grafana-migrator export --output-dir ...`")
    p.add_argument(
        "--target",
        choices=["operator", "api"],
        default="operator",
        help="Where to write. 'operator' (default) generates Grafana Operator CR manifests and "
        "dedups against the target cluster via kubectl. 'api' pushes straight to a target "
        "Grafana's HTTP API and needs no operator or kubectl -- use it for a bare Grafana or "
        "Grafana Cloud target. See docs/ADVANCED.md for the trade-offs.",
    )
    p.add_argument(
        "--namespace",
        default=None,
        help="Namespace holding the target Grafana Operator instance (required with --target operator)",
    )
    p.add_argument("--kube-context", default=None, help="kubectl context to use (default: current-context)")
    p.add_argument(
        "--instance-selector",
        default=None,
        type=parse_selector,
        help="Comma-separated key=value labels used as instanceSelector.matchLabels on every "
        "generated CR -- must match a label actually present on the target Grafana CR "
        "(e.g. dashboards=my-grafana). Required with --target operator; not used by --target api.",
    )
    p.add_argument(
        "--dest-url",
        default=os.environ.get("GRAFANA_DEST_URL"),
        help="Base URL of the target Grafana instance (default: $GRAFANA_DEST_URL). " "Required with --target api.",
    )
    p.add_argument(
        "--dest-path-segment",
        default=os.environ.get("GRAFANA_DEST_PATH_SEGMENT"),
        help="Ingress path prefix on the target, if any -- the --source-path-segment equivalent "
        "(default: $GRAFANA_DEST_PATH_SEGMENT).",
    )
    p.add_argument(
        "--dest-token",
        default=os.environ.get("GRAFANA_DEST_TOKEN"),
        help="Target Grafana service account token (default: $GRAFANA_DEST_TOKEN). Needs the "
        "Admin organization role: a lower role can create dashboards but silently fail on alert "
        "rules, contact points and the notification policy.",
    )
    p.add_argument(
        "--dest-user",
        default=os.environ.get("GRAFANA_DEST_USERNAME"),
        help="Target Grafana admin username (default: $GRAFANA_DEST_USERNAME), used if --dest-token is not given.",
    )
    p.add_argument(
        "--dest-password",
        default=os.environ.get("GRAFANA_DEST_PASSWORD"),
        help="Target Grafana admin password (default: $GRAFANA_DEST_PASSWORD).",
    )
    p.add_argument(
        "--editable",
        action="store_true",
        help="--target api only: send X-Disable-Provenance so migrated objects stay editable in "
        "the target's UI. Without it they are marked provisioned and are read-only there, which "
        "signals that the snapshot remains the source of truth.",
    )
    p.add_argument(
        "--stop-on-first-error",
        action="store_true",
        help="--target api only: abort on the first per-object failure instead of pushing what "
        "can be pushed and reporting the rest. Useful in CI.",
    )
    p.add_argument(
        "--output-dir",
        default="./grafana-migrator-import",
        help="Directory to write generated manifests + report.json into",
    )
    p.add_argument(
        "--include-title-duplicates",
        action="store_true",
        help="Also migrate dashboards whose title matches an existing one under a different uid "
        "(default: skip these and flag them for manual review)",
    )
    p.add_argument(
        "--skip-alerts",
        action="store_true",
        help="Don't import alert rules or contact points, even if the snapshot has them",
    )
    p.add_argument(
        "--skip-notification-policy",
        action="store_true",
        help="Don't import the notification policy tree, even if the snapshot has a custom one",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Discover + dedup + print the report, but don't write any files",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="After writing manifests, apply them immediately (incompatible with --dry-run). "
        "To apply an import later instead, without redoing dedup, use "
        "`grafana-migrator apply <output-dir>`.",
    )
    p.add_argument(
        "--secrets-file",
        default=None,
        help="YAML/JSON file of {contact point name: {secure field: value}}, supplying the "
        "credentials Grafana redacts on export. In operator mode these populate the generated "
        "Secret instead of leaving it blank. Run --write-secrets-skeleton first to get a file "
        "listing exactly the fields this import needs.",
    )
    p.add_argument(
        "--write-secrets-skeleton",
        default=None,
        metavar="PATH",
        help="Dedup against the target, write a fill-in-the-blanks secrets file covering the "
        "contact points this import would create, and exit without writing manifests.",
    )
    p.add_argument("--report-format", choices=["text", "json"], default="text")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def _validate_import_args(args: argparse.Namespace) -> Optional[str]:
    """First problem with this flag combination, or None.

    Kept as one function so the whole conditional matrix is testable without
    argparse, and so --namespace/--instance-selector can be required for one
    target and meaningless for the other.
    """
    if args.apply and args.dry_run:
        return "--apply cannot be combined with --dry-run"

    if args.target == "operator":
        if not args.namespace:
            return "--namespace is required with --target operator"
        if not args.instance_selector:
            return "--instance-selector is required with --target operator"
        return None

    if args.apply:
        return "--apply applies CR manifests, so it only works with --target operator"
    # Erroring rather than ignoring: a passed selector that silently did
    # nothing would read as "my CRs got these labels".
    if args.namespace or args.instance_selector:
        return "--namespace/--instance-selector only apply to --target operator"
    if not args.dest_url:
        return "--dest-url is required with --target api (or $GRAFANA_DEST_URL)"
    if not args.dest_token and not (args.dest_user and args.dest_password):
        return (
            "target credentials required with --target api (--dest-token / $GRAFANA_DEST_TOKEN, or "
            "--dest-user/--dest-password / $GRAFANA_DEST_USERNAME/$GRAFANA_DEST_PASSWORD)"
        )
    return None


def _write_secrets_skeleton(args: argparse.Namespace, plan: ImportPlan) -> int:
    path = Path(args.write_secrets_skeleton)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(secrets_skeleton(plan.contact_points_new))
    needing = [cp.name for cp in plan.contact_points_new if cp.secure_field_names]
    print(
        f"wrote a secrets skeleton for {len(needing)} contact point(s) to {path}"
        + (f" -- fill in: {', '.join(needing)}" if needing else "")
    )
    return 0


def run_import(argv: list[str] | None = None) -> int:
    args = build_import_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    problem = _validate_import_args(args)
    if problem:
        print(f"error: {problem}", file=sys.stderr)
        return 2

    try:
        dump = read_source_dump(Path(args.export_dir))
    except SourceDumpError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        secrets = load_secrets_file(Path(args.secrets_file)) if args.secrets_file else {}
    except SecretsFileError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    report = MigrationReport(backend=args.target)
    opts = PlanOptions(
        include_title_duplicates=args.include_title_duplicates,
        skip_alerts=args.skip_alerts,
        skip_notification_policy=args.skip_notification_policy,
    )
    # Skipping alerts means never reading the alerting endpoints at all, rather
    # than reading them and throwing the answer away.
    include_alerting = not (args.skip_alerts or dump.alert_rules_raw is None)

    if args.target == "operator":
        return _run_import_operator(args, dump, opts, include_alerting, secrets, report)
    return _run_import_api(args, dump, opts, include_alerting, secrets, report)


def _run_import_operator(
    args: argparse.Namespace,
    dump: Any,
    opts: PlanOptions,
    include_alerting: bool,
    secrets: dict[str, dict[str, str]],
    report: MigrationReport,
) -> int:
    try:
        inventory = build_kubectl_inventory(args.namespace, args.kube_context, include_alerting=include_alerting)
        plan = plan_import(dump, inventory, opts, report)
    except IncompleteSnapshotError as exc:
        print(
            f"error: {args.export_dir} has no dashboards/{exc.uid}.json, but search.json lists it "
            "-- snapshot looks incomplete or corrupted",
            file=sys.stderr,
        )
        return 1
    except KubectlError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.write_secrets_skeleton:
        return _write_secrets_skeleton(args, plan)

    report.warnings.extend(validate_secrets(secrets, plan.contact_points_new))
    manifests = emit_manifests(
        plan,
        namespace=args.namespace,
        instance_selector=args.instance_selector,
        report=report,
        secrets=secrets,
    )

    if not args.dry_run:
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        for rel_path, manifest in manifests:
            dest = out_dir / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(dump_manifest(manifest))
        (out_dir / "report.json").write_text(report.to_json())
        logger.info("wrote %d manifest(s) to %s", len(manifests), out_dir)

    print(report.to_json() if args.report_format == "json" else report.to_text())

    if args.apply:
        rc = _kubectl_apply_dirs(Path(args.output_dir), _manifest_subdirs(manifests), args.kube_context)
        if rc != 0:
            return rc

    return 0


def _run_import_api(
    args: argparse.Namespace,
    dump: Any,
    opts: PlanOptions,
    include_alerting: bool,
    secrets: dict[str, dict[str, str]],
    report: MigrationReport,
) -> int:
    try:
        client = build_client(
            url=args.dest_url,
            token=args.dest_token,
            user=args.dest_user,
            password=args.dest_password,
            path_segment=args.dest_path_segment,
            flag_prefix="dest",
            # Set once for the whole run rather than per call site: forgetting
            # it on one of four provisioning writes would be a silent change in
            # whether that object is editable.
            default_headers={"X-Disable-Provenance": "true"} if args.editable else None,
        )
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    try:
        inventory = build_grafana_inventory(client, include_alerting=include_alerting)
        plan = plan_import(dump, inventory, opts, report)
    except IncompleteSnapshotError as exc:
        print(
            f"error: {args.export_dir} has no dashboards/{exc.uid}.json, but search.json lists it "
            "-- snapshot looks incomplete or corrupted",
            file=sys.stderr,
        )
        return 1
    except GrafanaClientError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if args.write_secrets_skeleton:
        return _write_secrets_skeleton(args, plan)

    report.warnings.extend(validate_secrets(secrets, plan.contact_points_new))
    rc = push(
        plan,
        client,
        ApiPushOptions(
            dry_run=args.dry_run,
            stop_on_first_error=args.stop_on_first_error,
            secrets=secrets,
        ),
        report,
    )

    if not args.dry_run:
        # report.json is the record of what landed, and what to fix before a
        # re-run. Written even when the push partly failed -- especially then.
        out_dir = Path(args.output_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "report.json").write_text(report.to_json())
        logger.info("wrote report to %s", out_dir / "report.json")

    print(report.to_json() if args.report_format == "json" else report.to_text())
    return rc


# ---------------------------------------------------------------------------
# apply: re-apply a manifest directory `import` already wrote
# ---------------------------------------------------------------------------


def _kubectl_apply_dirs(base_dir: Path, subdirs: list[str], kube_context: Optional[str]) -> int:
    """Run `kubectl apply -R` scoped to just the given manifest subdirectories.

    Never point -f at base_dir itself -- it also holds report.json.
    """
    if not subdirs:
        logger.info("nothing to apply -- no manifest subdirectories found under %s", base_dir)
        return 0
    cmd = ["kubectl", "apply", "-R"]
    for subdir in subdirs:
        cmd += ["-f", str(base_dir / subdir)]
    if kube_context:
        cmd += ["--context", kube_context]
    logger.info("applying manifests: %s", " ".join(cmd))
    proc = subprocess.run(cmd)
    return proc.returncode


def _existing_manifest_subdirs(export_dir: Path) -> list[str]:
    """Subdirectories of a previously-written --output-dir containing at
    least one .yaml manifest. Naturally excludes report.json.
    """
    if not export_dir.is_dir():
        return []
    return sorted(p.name for p in export_dir.iterdir() if p.is_dir() and any(p.glob("*.yaml")))


def build_apply_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="grafana-migrator apply",
        description=(
            "Apply a manifest directory previously written by `grafana-migrator import --output-dir ...`, "
            "without re-running dedup against the target cluster. Useful for reviewing manifests before "
            "applying them, applying them at a later time, applying them to more than one cluster, or "
            "applying them from a machine that only has kubectl access."
        ),
    )
    p.add_argument("export_dir", help="Directory previously written by `grafana-migrator import --output-dir ...`")
    p.add_argument("--kube-context", default=None, help="kubectl context to use (default: current-context)")
    p.add_argument("-v", "--verbose", action="store_true")
    return p


def run_apply(argv: list[str]) -> int:
    args = build_apply_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    export_dir = Path(args.export_dir)
    if not export_dir.is_dir():
        print(f"error: {export_dir} is not a directory", file=sys.stderr)
        return 2

    subdirs = _existing_manifest_subdirs(export_dir)
    if not subdirs:
        print(
            f"error: no manifest subdirectories with .yaml files found under {export_dir} "
            "(expected e.g. dashboards/, folders/, alert-rules/, contact-points/, notification-policy/)",
            file=sys.stderr,
        )
        return 2

    return _kubectl_apply_dirs(export_dir, subdirs, args.kube_context)


# ---------------------------------------------------------------------------
# dispatch
# ---------------------------------------------------------------------------

_SUBCOMMAND_NAMES = ("export", "import", "apply")


def run(argv: list[str] | None = None) -> int:
    """Dispatch to `grafana-migrator export`, `import`, or `apply`."""
    argv = list(argv if argv is not None else sys.argv[1:])
    if not argv or argv[0] not in _SUBCOMMAND_NAMES:
        print(
            f"usage: grafana-migrator {{{','.join(_SUBCOMMAND_NAMES)}}} ...\n\n"
            "  export  fetch from a source Grafana instance, write a raw snapshot (no target cluster needed)\n"
            "  import  dedup a snapshot against a target cluster, write/apply CR manifests\n"
            "  apply   re-apply a manifest directory `import` already wrote\n",
            file=sys.stderr,
        )
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "export":
        return run_export(rest)
    if cmd == "import":
        return run_import(rest)
    return run_apply(rest)


def main() -> None:
    raise SystemExit(run())
