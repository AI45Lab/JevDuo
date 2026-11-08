"""Shared CLI helpers for one-knob parallelism defaults."""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ParallelismDefaults:
    """Derived run/controller limits for a requested task parallelism.

    Attributes:
        concurrency: Batch task concurrency.
        sandbox_create_concurrency: Sandbox create throttle.
        controller_submit_concurrency: Controller submit throttle.
        controller_status_concurrency: Controller status-poll throttle.
        prepare_buffer: Prepared sandbox ready buffer size.
        prepare_concurrency: Prepared sandbox create concurrency.
        runner_workers: Controller process worker count.
        worker_queue_max: Controller process worker queue cap.
    """

    concurrency: int
    sandbox_create_concurrency: int
    controller_submit_concurrency: int
    controller_status_concurrency: int
    prepare_buffer: int
    prepare_concurrency: int
    runner_workers: int
    worker_queue_max: int


def positive_parallelism(value: str) -> int:
    """Parse a strictly positive parallelism value for argparse.

    Args:
        value: Raw CLI value.

    Returns:
        Parsed positive integer.

    Raises:
        argparse.ArgumentTypeError: If the value is not positive.
    """

    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("parallelism must be a positive integer")
    return parsed


def derive_parallelism_defaults(parallelism: int) -> ParallelismDefaults:
    """Return conservative companion limits for ``--parallelism``.

    The ratios match the high-concurrency rescue-run shape that proved stable:
    120 task slots derive create=24, submit=36, status=12, prepared buffer=90,
    and prepare concurrency=36.

    Args:
        parallelism: Requested task parallelism.

    Returns:
        Companion concurrency defaults.

    Raises:
        ValueError: If ``parallelism`` is not positive.
    """

    if parallelism <= 0:
        raise ValueError("parallelism must be positive")
    return ParallelismDefaults(
        concurrency=parallelism,
        sandbox_create_concurrency=_ceil_ratio(parallelism, 1, 5),
        controller_submit_concurrency=_ceil_ratio(parallelism, 3, 10),
        controller_status_concurrency=_ceil_ratio(parallelism, 1, 10),
        prepare_buffer=_ceil_ratio(parallelism, 3, 4),
        prepare_concurrency=_ceil_ratio(parallelism, 3, 10),
        runner_workers=parallelism,
        worker_queue_max=parallelism,
    )


def add_parallelism_arg(parser: argparse.ArgumentParser) -> None:
    """Attach the shared one-knob parallelism flag to a parser.

    Args:
        parser: Parser to mutate with the shared flag.
    """

    parser.add_argument(
        "--parallelism",
        type=positive_parallelism,
        default=0,
        help=(
            "One-knob concurrency preset; fills unset task concurrency, sandbox/controller throttles, "
            "prepared-sandbox sizing, process runner workers, and worker queue caps"
        ),
    )


def apply_batch_parallelism_defaults(args: argparse.Namespace) -> None:
    """Fill unset batch/case run throttles from ``args.parallelism``.

    Args:
        args: Parsed CLI namespace mutated in place.
    """

    parallelism = int(getattr(args, "parallelism", 0) or 0)
    if parallelism <= 0:
        return
    defaults = derive_parallelism_defaults(parallelism)
    _set_if_unset(args, "concurrency", defaults.concurrency)
    _set_if_unset(args, "create_concurrency", defaults.sandbox_create_concurrency)
    _set_if_unset(args, "submit_concurrency", defaults.controller_submit_concurrency)
    _set_if_unset(args, "status_concurrency", defaults.controller_status_concurrency)
    if bool(getattr(args, "prepare_sandboxes", False)):
        _set_if_none(args, "prepare_buffer", defaults.prepare_buffer)
        _set_if_none(args, "prepare_concurrency", defaults.prepare_concurrency)


def apply_controller_parallelism_defaults(args: argparse.Namespace) -> None:
    """Fill unset controller process executor limits from ``args.parallelism``.

    Args:
        args: Parsed CLI namespace mutated in place.
    """

    parallelism = int(getattr(args, "parallelism", 0) or 0)
    if parallelism <= 0:
        return
    defaults = derive_parallelism_defaults(parallelism)
    _set_if_unset(args, "runner_workers", defaults.runner_workers)
    if getattr(args, "worker_queue_max", None) is None:
        setattr(args, "worker_queue_max", defaults.worker_queue_max)


def _ceil_ratio(value: int, numerator: int, denominator: int) -> int:
    return max(1, (value * numerator + denominator - 1) // denominator)


def _set_if_unset(args: argparse.Namespace, name: str, value: int) -> None:
    if int(getattr(args, name, 0) or 0) <= 0:
        setattr(args, name, value)


def _set_if_none(args: argparse.Namespace, name: str, value: int) -> None:
    if getattr(args, name, None) is None:
        setattr(args, name, value)
