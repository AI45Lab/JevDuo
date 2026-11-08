"""CLI commands for batch and sandbox operations."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from northcua.batch.maintenance.contracts import MaintenanceApplyResult
from northcua.batch.maintenance.operations import BatchMaintenanceOperations
from northcua.batch.operations import BatchOperations
from northcua.cli.parallelism_args import add_parallelism_arg, apply_batch_parallelism_defaults
from northcua.cli.prepared_sandbox_args import (
    add_prepared_sandbox_args,
    prepared_policy_from_args,
    prepared_policy_override_from_args,
)
from northcua.cli.retry_args import parse_retry_backoff_seconds
from northcua.sandboxes.operations import SandboxOperations


def register_batch_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register batch commands on the root parser.

    Args:
        subparsers: Root argparse subparser collection.
    """

    batch = subparsers.add_parser("batch", help="Run manifest-driven batches through a controller")
    batch_sub = batch.add_subparsers(dest="batch_command", required=True)
    _register_batch_manifest_commands(batch_sub)
    _register_batch_state_commands(batch_sub)
    _register_batch_maintenance_commands(batch_sub)


def _register_batch_manifest_commands(batch_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    validate = batch_sub.add_parser("validate", help="Validate a YAML or JSONL batch manifest")
    validate.add_argument("--manifest", required=True, help="YAML/JSONL batch manifest path")
    run = batch_sub.add_parser("run", help="Run a YAML or JSONL batch manifest")
    run.add_argument("--manifest", required=True, help="YAML/JSONL batch manifest path")
    run.add_argument("--controller-url", default="", help="Controller websocket URL override")
    run.add_argument("--token", default="", help="Submitter/device token override")
    add_parallelism_arg(run)
    run.add_argument("--concurrency", type=int, default=0, help="Maximum concurrent tasks")
    run.add_argument("--create-concurrency", type=int, default=0, help="Maximum concurrent sandbox creates")
    run.add_argument(
        "--submit-concurrency",
        type=int,
        default=0,
        help="Maximum concurrent controller submit handshakes; 0 means unconstrained",
    )
    run.add_argument(
        "--status-concurrency",
        type=int,
        default=0,
        help="Maximum concurrent controller status websocket requests; 0 derives a conservative cap",
    )
    run.add_argument(
        "--submit-jitter-seconds",
        type=float,
        default=0.0,
        help="Deterministic per-task jitter before controller submit handshake",
    )
    run.add_argument(
        "--device-readiness-timeout-seconds",
        type=float,
        default=0.0,
        help="Maximum seconds to wait for sandbox device workers; 0 uses the runner default",
    )
    run.add_argument(
        "--infra-retry-backoff-seconds",
        default="",
        help="Comma-separated retry delays for infra failures, e.g. 30,90,180,300; empty retries immediately",
    )
    run.add_argument("--output-dir", default="logs/batch", help="Batch output directory")
    run.add_argument("--dry-run", action="store_true", help="Validate and plan without submitting tasks")
    add_prepared_sandbox_args(run)
    run.add_argument(
        "--persist-token",
        action="store_true",
        help="Persist controller token in run_config.json for unattended resume",
    )


def _register_batch_state_commands(batch_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    for command_name in ("status", "resume", "cancel", "pause", "drain", "report", "monitor"):
        command = batch_sub.add_parser(command_name, help=f"{command_name.title()} a local batch state directory")
        command.add_argument("--batch-id", required=True, help="Batch id")
        command.add_argument("--output-dir", default="logs/batch", help="Batch output directory")
        if command_name == "resume":
            command.add_argument("--controller-url", default="", help="Controller websocket URL override")
            command.add_argument("--token", default="", help="Submitter/device token override")
            command.add_argument(
                "--controller-db", type=Path, default=None, help="Local controller.sqlite for DB-first status"
            )
            add_parallelism_arg(command)
            command.add_argument("--concurrency", type=int, default=0, help="Maximum concurrent tasks")
            command.add_argument("--create-concurrency", type=int, default=0, help="Maximum concurrent sandbox creates")
            command.add_argument(
                "--submit-concurrency",
                type=int,
                default=0,
                help="Maximum concurrent controller submit handshakes; 0 means unconstrained",
            )
            command.add_argument(
                "--status-concurrency",
                type=int,
                default=0,
                help="Maximum concurrent controller status websocket requests; 0 keeps persisted/derived value",
            )
            command.add_argument(
                "--submit-jitter-seconds",
                type=float,
                default=0.0,
                help="Deterministic per-task jitter before controller submit handshake",
            )
            command.add_argument(
                "--device-readiness-timeout-seconds",
                type=float,
                default=0.0,
                help="Maximum seconds to wait for sandbox device workers; 0 keeps the persisted/default value",
            )
            command.add_argument(
                "--infra-retry-backoff-seconds",
                default=None,
                help="Comma-separated retry delays for infra failures; omit to keep persisted value",
            )
            add_prepared_sandbox_args(command)
            command.add_argument(
                "--pre-resume-cleanup",
                dest="pre_resume_cleanup",
                action="store_true",
                default=True,
                help="Run repair-stale and controller residue cleanup before submitting work",
            )
            command.add_argument(
                "--no-pre-resume-cleanup",
                dest="pre_resume_cleanup",
                action="store_false",
                help="Skip the pre-resume controller residue cleanup hook",
            )
            command.add_argument(
                "--pre-resume-cleanup-provider",
                default="",
                help="Sandbox provider id used by the pre-resume cleanup hook; defaults to the manifest provider",
            )
            command.add_argument(
                "--selected-retry-plan",
                action="append",
                type=Path,
                default=[],
                help="Validated JSON plan granting retries to exact terminal tasks; may be repeated",
            )
        if command_name == "cancel":
            command.add_argument(
                "--graceful-cleanup",
                dest="graceful_cleanup",
                action="store_true",
                default=True,
                help="Best-effort interrupt/release/terminate cleanup for active controller residue",
            )
            command.add_argument(
                "--no-graceful-cleanup",
                dest="graceful_cleanup",
                action="store_false",
                help="Only mark the batch cancel flag",
            )
            command.add_argument(
                "--cleanup-provider", default="e2b", help="Sandbox provider id used by graceful cleanup"
            )
        if command_name in {"pause", "drain"}:
            command.add_argument("--actor", default="", help="Operator name written to batch control audit")
            command.add_argument("--reason", default="", help="Reason written to batch control audit")
        if command_name == "monitor":
            command.add_argument("--controller", default="", help="Optional controller ws/wss URL for live telemetry")
            command.add_argument("--token", default="", help="Submitter read token for controller telemetry")


def _register_batch_maintenance_commands(batch_sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    maintenance = batch_sub.add_parser("maintenance", help="Inspect and repair batch control-plane state")
    maintenance_sub = maintenance.add_subparsers(dest="maintenance_command", required=True)
    doctor = maintenance_sub.add_parser("doctor", help="Dry-run batch SQLite/controller/sandbox diagnostics")
    _add_batch_maintenance_common_args(doctor)
    doctor.add_argument("--provider", default="e2b", help="Sandbox provider id used for inventory")
    doctor.add_argument("--skip-sandboxes", action="store_true", help="Skip provider inventory during diagnostics")
    reconcile = maintenance_sub.add_parser("reconcile", help="Reconcile SQLite rows from controller status")
    _add_batch_maintenance_common_args(reconcile)
    reconcile.add_argument("--controller-url", default="", help="Controller websocket URL override")
    reconcile.add_argument("--token", default="", help="Submitter read token")
    reconcile.add_argument(
        "--controller-db", type=Path, default=None, help="Local controller.sqlite for DB-first status"
    )
    reconcile.add_argument("--case-id", default="", help="Optional single case id to reconcile")
    reconcile.add_argument("--apply", action="store_true", help="Apply planned SQLite reconciliations")
    repair = maintenance_sub.add_parser("repair-stale", help="Reset expired pre-controller in-flight rows")
    _add_batch_maintenance_common_args(repair)
    repair.add_argument("--older-than-seconds", type=float, default=300.0, help="Minimum stale row age")
    repair.add_argument("--case-id", default="", help="Optional single case id to repair")
    repair.add_argument("--apply", action="store_true", help="Apply planned stale-row repairs")
    cancel = maintenance_sub.add_parser("cancel-case", help="Cancel one case locally and at the controller")
    _add_batch_maintenance_common_args(cancel)
    cancel.add_argument("--case-id", required=True, help="Case/task id to cancel")
    cancel.add_argument("--controller-url", default="", help="Controller websocket URL override")
    cancel.add_argument("--token", default="", help="Submitter token")
    cancel.add_argument("--apply", action="store_true", help="Apply planned cancellation")
    terminate = maintenance_sub.add_parser("terminate-sandboxes", help="Terminate E2B sandboxes owned by a batch")
    _add_batch_maintenance_common_args(terminate)
    terminate.add_argument("--case-id", default="", help="Optional single case id sandbox scope")
    terminate.add_argument("--provider", default="e2b", help="Sandbox provider id")
    terminate.add_argument(
        "--include-connect-existing",
        action="store_true",
        help="Also terminate matching inventory rows without task-attempt metadata",
    )
    terminate.add_argument("--force-active", action="store_true", help="Allow termination for active SQLite leases")
    terminate.add_argument(
        "--terminate-concurrency",
        type=int,
        default=8,
        help="Maximum concurrent sandbox terminate calls",
    )
    terminate.add_argument("--apply", action="store_true", help="Apply planned sandbox terminations")
    sweep = maintenance_sub.add_parser(
        "sweep-provider-orphans",
        help="Terminate provider-visible old-attempt/orphan sandboxes protected by SQLite state",
    )
    _add_batch_maintenance_common_args(sweep)
    sweep.add_argument("--case-id", default="", help="Optional single case id sandbox scope")
    sweep.add_argument("--provider", default="e2b", help="Sandbox provider id")
    sweep.add_argument(
        "--include-connect-existing",
        action="store_true",
        help="Also terminate matching inventory rows without task-attempt metadata",
    )
    sweep.add_argument(
        "--terminate-concurrency",
        type=int,
        default=8,
        help="Maximum concurrent sandbox terminate calls",
    )
    sweep.add_argument("--apply", action="store_true", help="Apply planned provider orphan terminations")
    cleanup = maintenance_sub.add_parser(
        "cleanup-controller-residue",
        help="Clean stale controller/session/device residue for one batch",
    )
    _add_batch_maintenance_common_args(cleanup)
    cleanup.add_argument("--batch-db", type=Path, default=None, help="Explicit batch_state.sqlite path")
    cleanup.add_argument("--controller-db", type=Path, default=None, help="Explicit controller.sqlite path")
    cleanup.add_argument("--output-report-dir", type=Path, default=None, help="Directory for summary and TSV files")
    cleanup.add_argument(
        "--protect-prepared", action="store_true", default=True, help="Protect unassigned prepared rows"
    )
    cleanup.add_argument(
        "--no-protect-prepared",
        dest="protect_prepared",
        action="store_false",
        help="Do not protect unassigned prepared rows",
    )
    cleanup.add_argument("--provider", default="e2b", help="Sandbox provider id")
    cleanup.add_argument("--skip-provider", action="store_true", help="Skip provider inventory and termination")
    cleanup.add_argument(
        "--include-current-active",
        action="store_true",
        help="Interrupt current active batch rows; intended for explicit stop cleanup",
    )
    cleanup.add_argument(
        "--include-prepared",
        action="store_true",
        help="Terminate unassigned prepared buffer rows; intended for explicit stop cleanup",
    )
    cleanup.add_argument("--apply", action="store_true", help="Apply controller cleanup")


def register_sandbox_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register sandbox provider commands on the root parser.

    Args:
        subparsers: Root argparse subparser collection.
    """

    sandbox = subparsers.add_parser("sandbox", help="Build and probe sandbox providers")
    sandbox_sub = sandbox.add_subparsers(dest="sandbox_command", required=True)
    template = sandbox_sub.add_parser("template", help="Manage sandbox templates")
    template_sub = template.add_subparsers(dest="template_command", required=True)
    build = template_sub.add_parser("build", help="Build a sandbox template from YAML")
    build.add_argument("--spec", required=True, help="Template YAML spec path")
    doctor = template_sub.add_parser("doctor", help="Run a live template doctor probe")
    doctor.add_argument("--provider", default="e2b", help="Sandbox provider id")
    doctor.add_argument("--template", required=True, help="Provider template alias")
    doctor.add_argument("--desktop", action="store_true", help="Use desktop-capable provider API")
    doctor.add_argument("--python", default="python3.12", help="Template Python executable used by doctor imports")
    doctor.add_argument("--catalog", default="", help="Optional case catalog used for template-specific doctor checks")
    doctor.add_argument(
        "--template-profile",
        default="",
        help="Optional catalog template profile id; defaults to matching --template alias",
    )
    doctor.add_argument(
        "--binary",
        action="append",
        default=[],
        help="Binary to check; may be provided multiple times. Defaults to the NorthCUA desktop set.",
    )
    capacity = sandbox_sub.add_parser("capacity", help="Inspect sandbox provider capacity")
    capacity_sub = capacity.add_subparsers(dest="capacity_command", required=True)
    capacity_probe = capacity_sub.add_parser("probe", help="Passive capacity probe without creating sandboxes")
    capacity_probe.add_argument("--provider", default="e2b", help="Sandbox provider id")
    capacity_probe.add_argument("--template", default="", help="Template alias/id to count separately")
    capacity_probe.add_argument("--requested-concurrency", type=int, default=0, help="Requested task concurrency")
    capacity_probe.add_argument("--max-active", type=int, default=0, help="Configured active sandbox limit")
    capacity_probe.add_argument("--reserve", type=int, default=0, help="Capacity to reserve for other workloads")
    capacity_probe.add_argument("--create-rate", type=int, default=0, help="Configured create rate per second")
    capacity_probe.add_argument(
        "--metadata",
        action="append",
        default=[],
        help="Metadata filter as key=value; may be provided multiple times",
    )
    capacity_probe.add_argument("--include-items", action="store_true", help="Include inventory rows in JSON output")
    probe = sandbox_sub.add_parser("probe", help="Run a provider smoke probe")
    probe.add_argument("--provider", default="e2b", help="Sandbox provider id")
    probe.add_argument("--template", default="osworld_desktop", help="Provider template alias")
    probe.add_argument("--desktop", action="store_true", help="Use desktop-capable provider API")
    list_cmd = sandbox_sub.add_parser("list", help="List registered sandbox providers")
    list_cmd.add_argument("--json", action="store_true", help="Output as JSON")


async def run_batch_command(args: argparse.Namespace) -> int:
    """Dispatch one batch CLI command.

    Args:
        args: Parsed argparse namespace.

    Returns:
        Process-style exit code.

    Raises:
        ValueError: If argparse produced an unsupported batch subcommand.
    """

    operations = BatchOperations()
    maintenance_operations = BatchMaintenanceOperations()
    output_dir = Path(getattr(args, "output_dir", "logs/batch"))
    if args.batch_command == "validate":
        manifest = operations.validate_manifest(args.manifest)
        print(json.dumps(manifest.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0
    if args.batch_command == "run":
        apply_batch_parallelism_defaults(args)
        result = await operations.run(
            manifest_path=args.manifest,
            output_dir=output_dir,
            controller_url=args.controller_url,
            token=args.token,
            concurrency=args.concurrency,
            sandbox_create_concurrency=args.create_concurrency,
            controller_submit_concurrency=args.submit_concurrency,
            controller_status_concurrency=args.status_concurrency,
            controller_submit_jitter_seconds=args.submit_jitter_seconds,
            device_readiness_timeout_seconds=args.device_readiness_timeout_seconds,
            infra_retry_backoff_seconds=parse_retry_backoff_seconds(args.infra_retry_backoff_seconds),
            prepared_sandbox_policy=prepared_policy_from_args(args),
            dry_run=args.dry_run,
            persist_token=args.persist_token,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0 if result.failed == 0 and result.canceled == 0 and result.incomplete == 0 else 1
    if args.batch_command == "status":
        states = operations.status(batch_id=args.batch_id, output_dir=output_dir)
        print(json.dumps([state.model_dump(mode="json") for state in states], indent=2, ensure_ascii=False))
        return 0
    if args.batch_command == "report":
        print(
            json.dumps(operations.report(batch_id=args.batch_id, output_dir=output_dir), indent=2, ensure_ascii=False)
        )
        return 0
    if args.batch_command == "monitor":
        if args.controller:
            from northcua.submitter.client import ControllerSubmitterClient
            from northcua_protocol.payloads import TelemetrySubscribeRequest

            async with ControllerSubmitterClient(args.controller) as client:
                update = await client.telemetry_update(
                    TelemetrySubscribeRequest(token=args.token, batch_id=args.batch_id, include_snapshot=True)
                )
            print(json.dumps(update.model_dump(mode="json"), indent=2, ensure_ascii=False))
            return 0
        print(
            json.dumps(operations.monitor(batch_id=args.batch_id, output_dir=output_dir), indent=2, ensure_ascii=False)
        )
        return 0
    if args.batch_command == "resume":
        try:
            apply_batch_parallelism_defaults(args)
            result = await operations.resume(
                batch_id=args.batch_id,
                output_dir=output_dir,
                controller_url=args.controller_url,
                token=args.token,
                concurrency=args.concurrency,
                sandbox_create_concurrency=args.create_concurrency,
                controller_submit_concurrency=args.submit_concurrency,
                controller_status_concurrency=args.status_concurrency,
                controller_submit_jitter_seconds=args.submit_jitter_seconds,
                device_readiness_timeout_seconds=args.device_readiness_timeout_seconds,
                infra_retry_backoff_seconds=(
                    parse_retry_backoff_seconds(args.infra_retry_backoff_seconds)
                    if args.infra_retry_backoff_seconds is not None
                    else None
                ),
                controller_db=args.controller_db,
                prepared_sandbox_policy=prepared_policy_override_from_args(args),
                pre_resume_cleanup=args.pre_resume_cleanup,
                pre_resume_cleanup_provider=args.pre_resume_cleanup_provider,
                selected_retry_plan_paths=args.selected_retry_plan,
            )
        except FileNotFoundError as exc:
            print(str(exc))
            return 1
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0 if result.failed == 0 and result.canceled == 0 and result.incomplete == 0 else 1
    if args.batch_command == "cancel":
        cancel_path = operations.cancel(batch_id=args.batch_id, output_dir=output_dir)
        cleanup = {}
        if args.graceful_cleanup:
            controller_db = output_dir.parent / "controller.sqlite"
            batch_db = output_dir / args.batch_id / "batch_state.sqlite"
            if batch_db.exists() and controller_db.exists():
                cleanup = await maintenance_operations.cleanup_controller_residue(
                    batch_id=args.batch_id,
                    output_dir=output_dir,
                    batch_db=batch_db,
                    controller_db=controller_db,
                    protect_prepared=False,
                    include_current_active=True,
                    include_prepared=True,
                    provider=args.cleanup_provider,
                    apply=True,
                )
        print(json.dumps({"cancel_requested": str(cancel_path), "cleanup": cleanup}, indent=2, ensure_ascii=False))
        return 0
    if args.batch_command == "pause":
        pause_path = operations.pause(
            batch_id=args.batch_id,
            output_dir=output_dir,
            actor=args.actor,
            reason=args.reason,
        )
        print(f"paused={pause_path}")
        return 0
    if args.batch_command == "drain":
        drain_path = operations.drain(
            batch_id=args.batch_id,
            output_dir=output_dir,
            actor=args.actor,
            reason=args.reason,
        )
        print(f"drain_requested={drain_path}")
        return 0
    if args.batch_command == "maintenance":
        result = await _run_batch_maintenance_command(args, output_dir=output_dir, operations=maintenance_operations)
        if isinstance(result, MaintenanceApplyResult):
            print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
            return 0 if not result.errors else 1
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0 if not result.get("warnings") else 0
    raise ValueError(f"Unsupported batch command: {args.batch_command}")


def run_sandbox_command(args: argparse.Namespace) -> int:
    """Dispatch one sandbox CLI command.

    Args:
        args: Parsed argparse namespace.

    Returns:
        Process-style exit code.

    Raises:
        ValueError: If argparse produced an unsupported sandbox subcommand.
    """

    operations = SandboxOperations()
    if args.sandbox_command == "list":
        providers = [provider.model_dump(mode="json") for provider in operations.list_providers()]
        if args.json:
            print(json.dumps(providers, indent=2, ensure_ascii=False))
        else:
            for provider in providers:
                print(f"{provider['provider']}: {', '.join(provider['features'])}")
        return 0
    if args.sandbox_command == "template" and args.template_command == "build":
        alias = asyncio.run(operations.build_template(args.spec))
        print(f"template={alias}")
        return 0
    if args.sandbox_command == "template" and args.template_command == "doctor":
        report = asyncio.run(
            operations.doctor(
                provider_id=args.provider,
                template=args.template,
                desktop=args.desktop,
                checked_binaries=args.binary or None,
                python_executable=args.python,
                catalog_path=args.catalog,
                template_profile=args.template_profile,
            )
        )
        print(json.dumps(report.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0 if report.healthy else 1
    if args.sandbox_command == "capacity" and args.capacity_command == "probe":
        from northcua.sandboxes.contracts import SandboxCapacityProbeRequest

        result = asyncio.run(
            operations.capacity_probe(
                SandboxCapacityProbeRequest(
                    provider=args.provider,
                    template=args.template,
                    requested_concurrency=args.requested_concurrency,
                    max_active_sandboxes=args.max_active,
                    reserve_sandboxes=args.reserve,
                    create_rate_per_second=args.create_rate,
                    metadata_filter=_parse_metadata_filters(args.metadata),
                    include_items=args.include_items,
                )
            )
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0
    if args.sandbox_command == "probe":
        result = asyncio.run(operations.probe(provider_id=args.provider, template=args.template, desktop=args.desktop))
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0 if result.ok else 1
    raise ValueError(f"Unsupported sandbox command: {args.sandbox_command}")


def _parse_metadata_filters(values: list[str]) -> dict[str, object]:
    filters: dict[str, object] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"metadata filter must be key=value: {value}")
        key, raw = value.split("=", 1)
        if not key:
            raise ValueError(f"metadata filter key cannot be empty: {value}")
        filters[key] = raw
    return filters


def _add_batch_maintenance_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--batch-id", required=True, help="Batch id")
    parser.add_argument("--output-dir", default="logs/batch", help="Batch output directory")


async def _run_batch_maintenance_command(
    args: argparse.Namespace,
    *,
    output_dir: Path,
    operations: BatchMaintenanceOperations,
) -> MaintenanceApplyResult | dict[str, object]:
    if args.maintenance_command == "doctor":
        return await operations.doctor(
            batch_id=args.batch_id,
            output_dir=output_dir,
            provider=args.provider,
            skip_sandboxes=args.skip_sandboxes,
        )
    if args.maintenance_command == "reconcile":
        return await operations.reconcile(
            batch_id=args.batch_id,
            output_dir=output_dir,
            controller_url=args.controller_url,
            token=args.token,
            controller_db=args.controller_db,
            case_id=args.case_id,
            apply=args.apply,
        )
    if args.maintenance_command == "repair-stale":
        return await operations.repair_stale(
            batch_id=args.batch_id,
            output_dir=output_dir,
            older_than_seconds=args.older_than_seconds,
            case_id=args.case_id,
            apply=args.apply,
        )
    if args.maintenance_command == "cancel-case":
        return await operations.cancel_case(
            batch_id=args.batch_id,
            output_dir=output_dir,
            case_id=args.case_id,
            controller_url=args.controller_url,
            token=args.token,
            apply=args.apply,
        )
    if args.maintenance_command == "terminate-sandboxes":
        return await operations.terminate_sandboxes(
            batch_id=args.batch_id,
            output_dir=output_dir,
            provider=args.provider,
            case_id=args.case_id,
            include_connect_existing=args.include_connect_existing,
            force_active=args.force_active,
            terminate_concurrency=args.terminate_concurrency,
            apply=args.apply,
        )
    if args.maintenance_command == "sweep-provider-orphans":
        return await operations.sweep_provider_orphans(
            batch_id=args.batch_id,
            output_dir=output_dir,
            provider=args.provider,
            case_id=args.case_id,
            include_connect_existing=args.include_connect_existing,
            terminate_concurrency=args.terminate_concurrency,
            apply=args.apply,
        )
    if args.maintenance_command == "cleanup-controller-residue":
        return await operations.cleanup_controller_residue(
            batch_id=args.batch_id,
            output_dir=output_dir,
            batch_db=args.batch_db,
            controller_db=args.controller_db,
            output_report_dir=args.output_report_dir,
            protect_prepared=args.protect_prepared,
            include_current_active=args.include_current_active,
            include_prepared=args.include_prepared,
            provider=args.provider,
            skip_provider=args.skip_provider,
            apply=args.apply,
        )
    raise ValueError(f"Unsupported batch maintenance command: {args.maintenance_command}")
