"""CLI commands for reusable case suites."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from northcua.cli.controller_defaults import PUBLIC_CONTROLLER_URL_ENV, default_public_controller_url
from northcua.cli.parallelism_args import add_parallelism_arg, apply_batch_parallelism_defaults
from northcua.cli.prepared_sandbox_args import add_prepared_sandbox_args, prepared_policy_override_from_args
from northcua.cli.retry_args import parse_retry_backoff_seconds
from northcua.experiments.cases.operations import CaseOperations
from northcua.experiments.cases.runtime import CaseControllerRuntime


def register_case_commands(subparsers: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """Register case-suite commands on the root parser.

    Args:
        subparsers: Root CLI subparser collection that receives ``case``.
    """

    case = subparsers.add_parser("case", help="Materialize and run reusable CUA case suites")
    case_sub = case.add_subparsers(dest="case_command", required=True)

    validate = case_sub.add_parser("validate", help="Validate a case suite spec")
    validate.add_argument("--spec", required=True, help="Case suite YAML path")
    validate.add_argument("--catalog", default="", help="Optional app/template catalog path")

    materialize = case_sub.add_parser("materialize", help="Materialize a case suite without running it")
    materialize.add_argument("--spec", required=True, help="Case suite YAML path")
    materialize.add_argument("--catalog", default="", help="Optional app/template catalog path")
    materialize.add_argument("--output-dir", default="logs/cases", help="Materialized output root")
    materialize.add_argument(
        "--public-controller-url",
        default=default_public_controller_url(),
        help=f"Controller URL to store in the manifest; defaults to ${PUBLIC_CONTROLLER_URL_ENV}",
    )
    materialize.add_argument("--run-id", default="", help="Optional materialized run id namespace")

    run = case_sub.add_parser("run", help="Run a case suite through the batch runner")
    run.add_argument("--spec", required=True, help="Case suite YAML path")
    run.add_argument("--catalog", default="", help="Optional app/template catalog path")
    run.add_argument("--output-dir", default="logs/cases", help="Case output root")
    run.add_argument(
        "--public-controller-url",
        default=default_public_controller_url(),
        help=f"Public wss:// URL reachable from sandboxes; defaults to ${PUBLIC_CONTROLLER_URL_ENV}",
    )
    run.add_argument("--run-id", default="", help="Explicit run id for resume or deterministic output")
    run.add_argument("--resume", action="store_true", help="Resume an existing case run by --run-id")
    run.add_argument("--force", action="store_true", help="Remove and recreate an existing run directory")
    run.add_argument("--dry-run", action="store_true", help="Materialize and plan without external side effects")
    add_parallelism_arg(run)
    run.add_argument("--concurrency", type=int, default=0, help="Maximum concurrent case tasks")
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
        "--lease-timeout-seconds",
        type=float,
        default=0.0,
        help="Local task lease timeout for crash takeover; 0 uses the runner default",
    )
    run.add_argument(
        "--infra-retry-backoff-seconds",
        default="",
        help="Comma-separated retry delays for infra failures, e.g. 30,90,180,300; empty retries immediately",
    )
    add_prepared_sandbox_args(run)

    for command_name in ("status", "monitor", "report", "cancel", "pause", "drain"):
        command = case_sub.add_parser(command_name, help=f"{command_name.title()} a persisted case run")
        command.add_argument("--suite-id", required=True, help="Case suite id")
        command.add_argument("--run-id", required=True, help="Case run id")
        command.add_argument("--output-dir", default="logs/cases", help="Case output root")


async def run_case_command(args: argparse.Namespace) -> int:
    """Dispatch one case-suite command.

    Args:
        args: Parsed argparse namespace produced by register_case_commands.

    Returns:
        Process-style exit code for the requested command.

    Raises:
        ValueError: When an unknown case subcommand is requested.
    """

    operations = CaseOperations()
    if args.case_command == "validate":
        spec = operations.validate(args.spec, catalog_path=args.catalog or None)
        print(CaseControllerRuntime().spec_for_persistence(spec).model_dump_json(indent=2))
        return 0
    if args.case_command == "materialize":
        materialized = operations.materialize_file(
            spec_path=args.spec,
            output_dir=Path(args.output_dir),
            catalog_path=args.catalog or None,
            public_controller_url=args.public_controller_url,
            run_id=args.run_id,
        )
        print(materialized.manifest.model_dump_json(indent=2))
        return 0
    if args.case_command == "run":
        apply_batch_parallelism_defaults(args)
        result = await operations.run_file(
            spec_path=args.spec,
            output_dir=Path(args.output_dir),
            catalog_path=args.catalog or None,
            public_controller_url=args.public_controller_url,
            dry_run=args.dry_run,
            run_id=args.run_id,
            resume=args.resume,
            force=args.force,
            concurrency=args.concurrency,
            controller_parallelism=args.parallelism,
            sandbox_create_concurrency=args.create_concurrency,
            controller_submit_concurrency=args.submit_concurrency,
            controller_status_concurrency=args.status_concurrency,
            controller_submit_jitter_seconds=args.submit_jitter_seconds,
            device_readiness_timeout_seconds=args.device_readiness_timeout_seconds,
            lease_timeout_seconds=args.lease_timeout_seconds,
            infra_retry_backoff_seconds=parse_retry_backoff_seconds(args.infra_retry_backoff_seconds),
            prepared_sandbox_policy=prepared_policy_override_from_args(args),
        )
        print(result.model_dump_json(indent=2))
        return 0 if result.failed == 0 and result.canceled == 0 and result.incomplete == 0 else 1
    if args.case_command == "status":
        states = operations.status(suite_id=args.suite_id, run_id=args.run_id, output_dir=Path(args.output_dir))
        print(json.dumps([state.model_dump(mode="json") for state in states], indent=2, ensure_ascii=False))
        return 0
    if args.case_command == "monitor":
        print(
            json.dumps(
                operations.monitor(suite_id=args.suite_id, run_id=args.run_id, output_dir=Path(args.output_dir)),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    if args.case_command == "report":
        print(
            json.dumps(
                operations.report(suite_id=args.suite_id, run_id=args.run_id, output_dir=Path(args.output_dir)),
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0
    if args.case_command == "cancel":
        cancel_path = operations.cancel(suite_id=args.suite_id, run_id=args.run_id, output_dir=Path(args.output_dir))
        print(f"cancel_requested={cancel_path}")
        return 0
    if args.case_command == "pause":
        pause_path = operations.pause(suite_id=args.suite_id, run_id=args.run_id, output_dir=Path(args.output_dir))
        print(f"paused={pause_path}")
        return 0
    if args.case_command == "drain":
        drain_path = operations.drain(suite_id=args.suite_id, run_id=args.run_id, output_dir=Path(args.output_dir))
        print(f"drain_requested={drain_path}")
        return 0
    raise ValueError(f"Unsupported case command: {args.case_command}")
