"""CLI commands for experiment suites."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from northcua.cli.controller_defaults import PUBLIC_CONTROLLER_URL_ENV, default_public_controller_url
from northcua.cli.parallelism_args import add_parallelism_arg, apply_batch_parallelism_defaults
from northcua.cli.prepared_sandbox_args import add_prepared_sandbox_args, prepared_policy_override_from_args
from northcua.cli.retry_args import parse_retry_backoff_seconds
from northcua.experiments.orchestration import ExperimentOperations


def register_experiment_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register experiment suite commands on the root parser.

    Args:
        subparsers: Root argparse subparser collection.
    """

    experiment = subparsers.add_parser("experiment", help="Run high-level CUA experiment suites")
    experiment_sub = experiment.add_subparsers(dest="experiment_command", required=True)

    validate = experiment_sub.add_parser("validate", help="Validate an experiment spec")
    validate.add_argument("--spec", required=True, help="Experiment YAML/JSON spec path")

    run = experiment_sub.add_parser("run", help="Run an experiment spec")
    run.add_argument("--spec", required=True, help="Experiment YAML/JSON spec path")
    run.add_argument(
        "--public-controller-url",
        default=default_public_controller_url(),
        help=f"Public wss:// URL reachable from sandboxes; defaults to ${PUBLIC_CONTROLLER_URL_ENV}",
    )
    run.add_argument("--output-dir", default="logs/experiments", help="Experiment output root")
    run.add_argument("--dry-run", action="store_true", help="Plan without creating sandboxes or submitting tasks")
    run.add_argument(
        "--require-real-e2b",
        action="store_true",
        help="Deprecated; real providers always require NORTHCUA_ALLOW_LIVE_SANDBOXES=1",
    )

    resume = experiment_sub.add_parser("resume", help="Resume an interrupted experiment")
    resume.add_argument("--experiment-id", required=True, help="Experiment id")
    resume.add_argument("--output-dir", default="logs/experiments", help="Experiment output root")
    add_parallelism_arg(resume)
    resume.add_argument("--concurrency", type=int, default=0, help="Resume-time concurrency override")
    resume.add_argument("--create-concurrency", type=int, default=0, help="Maximum concurrent sandbox creates")
    resume.add_argument(
        "--submit-concurrency",
        type=int,
        default=0,
        help="Maximum concurrent controller submit handshakes; 0 keeps persisted/derived value",
    )
    resume.add_argument(
        "--status-concurrency",
        type=int,
        default=0,
        help="Maximum concurrent controller status websocket requests; 0 keeps persisted/derived value",
    )
    resume.add_argument(
        "--submit-jitter-seconds",
        type=float,
        default=0.0,
        help="Deterministic per-task jitter before controller submit handshake",
    )
    resume.add_argument(
        "--device-readiness-timeout-seconds",
        type=float,
        default=0.0,
        help="Maximum seconds to wait for sandbox device workers; 0 keeps the runner default",
    )
    resume.add_argument(
        "--infra-retry-backoff-seconds",
        default=None,
        help="Comma-separated retry delays for infra failures, e.g. 30,90,180,300; empty retries immediately",
    )
    add_prepared_sandbox_args(resume)
    resume.add_argument(
        "--public-controller-url",
        default="",
        help=(
            "Optional public wss:// URL override reachable from sandboxes; "
            f"omitted preserves the persisted experiment URL instead of ${PUBLIC_CONTROLLER_URL_ENV}"
        ),
    )

    recover_plan = experiment_sub.add_parser("recover-plan", help="Plan experiment recovery without side effects")
    recover_plan.add_argument("--experiment-id", required=True, help="Experiment id")
    recover_plan.add_argument("--output-dir", default="logs/experiments", help="Experiment output root")
    recover_plan.add_argument("--format", choices=("json", "md"), default="json", help="Output format")

    for command_name in ("status", "report", "monitor", "audit", "pause", "drain", "cancel"):
        command = experiment_sub.add_parser(command_name, help=f"{command_name.title()} an experiment output")
        command.add_argument("--experiment-id", required=True, help="Experiment id")
        command.add_argument("--output-dir", default="logs/experiments", help="Experiment output root")
        if command_name == "monitor":
            command.add_argument("--controller", default="", help="Optional controller ws/wss URL for live telemetry")
            command.add_argument("--token", default="", help="Submitter read token for controller telemetry")
        if command_name == "audit":
            command.add_argument("--format", choices=("json", "md"), default="json", help="Audit output format")


async def run_experiment_command(args: argparse.Namespace) -> int:
    """Dispatch one experiment CLI command.

    Args:
        args: Parsed argparse namespace.

    Returns:
        Process-style exit code.

    Raises:
        ValueError: If argparse produced an unsupported experiment command.
    """

    operations = ExperimentOperations()
    output_dir = Path(getattr(args, "output_dir", "logs/experiments"))
    if args.experiment_command == "validate":
        spec = operations.validate(args.spec)
        print(json.dumps(spec.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0
    if args.experiment_command == "run":
        result = await operations.run_file(
            spec_path=args.spec,
            output_dir=output_dir,
            public_controller_url=args.public_controller_url,
            dry_run=args.dry_run,
            require_real_e2b=True,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return _result_exit_code(result, dry_run=args.dry_run)
    return await _run_persisted_experiment_command(args=args, operations=operations, output_dir=output_dir)


async def _run_persisted_experiment_command(
    *,
    args: argparse.Namespace,
    operations: ExperimentOperations,
    output_dir: Path,
) -> int:
    """Dispatch commands that operate on an existing experiment directory.

    Args:
        args: Parsed argparse namespace.
        operations: Experiment operations facade.
        output_dir: Root directory containing experiment outputs.

    Returns:
        Process-style exit code.

    Raises:
        ValueError: If the command is not supported.
    """

    if args.experiment_command == "status":
        print(
            json.dumps(
                operations.status(experiment_id=args.experiment_id, output_dir=output_dir),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    if args.experiment_command == "report":
        return _run_report_command(args=args, operations=operations, output_dir=output_dir)
    if args.experiment_command == "monitor":
        return await _run_monitor_command(args=args, operations=operations, output_dir=output_dir)
    if args.experiment_command == "audit":
        audit = operations.audit(
            experiment_id=args.experiment_id,
            output_dir=output_dir,
            output_format=args.format,
        )
        print(audit if isinstance(audit, str) else json.dumps(audit, indent=2, ensure_ascii=False))
        return 0
    if args.experiment_command == "recover-plan":
        plan = operations.recover_plan(experiment_id=args.experiment_id, output_dir=output_dir)
        print(_plan_markdown(plan) if args.format == "md" else plan.model_dump_json(indent=2))
        return 0
    if args.experiment_command == "resume":
        apply_batch_parallelism_defaults(args)
        result = await operations.resume(
            experiment_id=args.experiment_id,
            output_dir=output_dir,
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
            prepared_sandbox_policy=prepared_policy_override_from_args(args),
            public_controller_url=args.public_controller_url,
        )
        print(json.dumps(result.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return _result_exit_code(result, dry_run=False)
    if args.experiment_command == "pause":
        print(str(operations.pause(experiment_id=args.experiment_id, output_dir=output_dir)))
        return 0
    if args.experiment_command == "drain":
        print(str(operations.drain(experiment_id=args.experiment_id, output_dir=output_dir)))
        return 0
    if args.experiment_command == "cancel":
        print(str(operations.cancel(experiment_id=args.experiment_id, output_dir=output_dir)))
        return 0
    raise ValueError(f"Unsupported experiment command: {args.experiment_command}")


def _run_report_command(
    *,
    args: argparse.Namespace,
    operations: ExperimentOperations,
    output_dir: Path,
) -> int:
    try:
        report = operations.report(experiment_id=args.experiment_id, output_dir=output_dir)
    except FileNotFoundError as exc:
        print(str(exc))
        return 1
    print(json.dumps(report, indent=2, ensure_ascii=False))
    return 0


async def _run_monitor_command(
    *,
    args: argparse.Namespace,
    operations: ExperimentOperations,
    output_dir: Path,
) -> int:
    if args.controller:
        from northcua.submitter.client import ControllerSubmitterClient
        from northcua_protocol.payloads import TelemetrySubscribeRequest

        async with ControllerSubmitterClient(args.controller) as client:
            update = await client.telemetry_update(
                TelemetrySubscribeRequest(
                    token=args.token or os.getenv("NORTHCUA_CONTROLLER_TOKEN", ""),
                    experiment_id=args.experiment_id,
                    include_snapshot=True,
                )
            )
        print(json.dumps(update.model_dump(mode="json"), indent=2, ensure_ascii=False))
        return 0
    print(
        json.dumps(
            operations.monitor(experiment_id=args.experiment_id, output_dir=output_dir),
            indent=2,
            ensure_ascii=False,
        )
    )
    return 0


def _result_exit_code(result, *, dry_run: bool) -> int:
    if dry_run:
        return 0
    if result.batch_result.failed or result.batch_result.canceled:
        return 1
    return 0 if all(row.passed for row in result.task_results) else 1


def _plan_markdown(plan) -> str:
    lines = [
        f"# Recovery Plan: {plan.experiment_id}",
        "",
        f"- batch: `{plan.batch_id}`",
        f"- runnable cases: {plan.runnable_count}",
        "",
        "| task | status | action | attempts | remaining | reason |",
        "| --- | --- | --- | ---: | ---: | --- |",
    ]
    for item in plan.items:
        lines.append(
            f"| `{item.task_id}` | {item.current_status.value} | {item.action.value} | "
            f"{item.attempts} | {item.retry_budget_remaining} | {item.reason} |"
        )
    return "\n".join(lines)
