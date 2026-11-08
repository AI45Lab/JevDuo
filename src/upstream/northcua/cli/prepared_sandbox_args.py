"""Argparse helpers for prepared sandbox queue flags."""

from __future__ import annotations

import argparse

from northcua.batch.preparation import PreparedSandboxDepth, PreparedSandboxPolicy

DEFAULT_PREPARE_CLAIM_WAIT_SECONDS = 3600.0


def add_prepared_sandbox_args(parser: argparse.ArgumentParser) -> None:
    """Attach prepared sandbox queue flags to a run-like command.

    Args:
        parser: Argparse parser for ``case run`` or ``batch run/resume``.
    """

    parser.add_argument(
        "--prepare-sandboxes",
        action="store_true",
        help="Enable the single-use prepared sandbox queue",
    )
    parser.add_argument(
        "--prepare-depth",
        choices=[item.value for item in PreparedSandboxDepth],
        default=None,
        help="How much case initialization to run before task claim",
    )
    parser.add_argument("--prepare-buffer", type=int, default=None, help="Prepared sandbox target buffer size")
    parser.add_argument(
        "--prepare-concurrency", type=int, default=None, help="Concurrent background prepare operations"
    )
    parser.add_argument(
        "--prepare-initial-warmup-wait-seconds",
        type=float,
        default=None,
        help="Maximum batch-start seconds to wait for the first prepared sandbox before claim fallback",
    )
    parser.add_argument("--prepare-ttl-seconds", type=float, default=None, help="Prepared sandbox TTL")
    parser.add_argument(
        "--prepare-claim-wait-seconds",
        type=float,
        default=None,
        help=(
            "Upper-bound seconds for legacy prepared claim waits; "
            "--prepare-claim-pending-wait-seconds controls foreground pending waits"
        ),
    )
    parser.add_argument(
        "--prepare-claim-pending-wait-seconds",
        type=float,
        default=None,
        help=(
            "Foreground seconds to wait when a claim sees an in-flight prepared future; "
            "zero fails open to normal sandbox creation"
        ),
    )
    parser.add_argument(
        "--prepare-min-assignment-ttl-seconds",
        type=float,
        default=None,
        help=(
            "Minimum remaining prepared lease TTL required at assignment; "
            "default derives from task timeout plus assignment TTL margin"
        ),
    )
    parser.add_argument(
        "--prepare-assignment-ttl-margin-seconds",
        type=float,
        default=None,
        help="Extra seconds added to task timeout for prepared assignment TTL guard",
    )


def prepared_policy_from_args(args: argparse.Namespace) -> PreparedSandboxPolicy:
    """Build a typed prepared sandbox policy from argparse fields.

    Args:
        args: Parsed command-line namespace containing prepare flags.

    Returns:
        Policy represented by CLI flag values.
    """

    ttl_seconds = getattr(args, "prepare_ttl_seconds", None)
    claim_wait_seconds = getattr(args, "prepare_claim_wait_seconds", None)
    claim_pending_wait_seconds = getattr(args, "prepare_claim_pending_wait_seconds", None)
    initial_warmup_wait_seconds = getattr(args, "prepare_initial_warmup_wait_seconds", None)
    min_assignment_ttl_seconds = getattr(args, "prepare_min_assignment_ttl_seconds", None)
    assignment_ttl_margin_seconds = getattr(args, "prepare_assignment_ttl_margin_seconds", None)
    prepare_depth = getattr(args, "prepare_depth", None) or PreparedSandboxDepth.FULL_INIT.value
    prepare_buffer = getattr(args, "prepare_buffer", None)
    prepare_concurrency = getattr(args, "prepare_concurrency", None)
    return PreparedSandboxPolicy(
        enabled=bool(getattr(args, "prepare_sandboxes", False)),
        depth=PreparedSandboxDepth(str(prepare_depth)),
        buffer=int(prepare_buffer if prepare_buffer is not None else 0),
        prepare_concurrency=int(prepare_concurrency if prepare_concurrency is not None else 0),
        initial_warmup_wait_seconds=float(
            PreparedSandboxPolicy().initial_warmup_wait_seconds
            if initial_warmup_wait_seconds is None
            else initial_warmup_wait_seconds
        ),
        ttl_seconds=float(600.0 if ttl_seconds is None else ttl_seconds),
        claim_wait_seconds=float(
            DEFAULT_PREPARE_CLAIM_WAIT_SECONDS if claim_wait_seconds is None else claim_wait_seconds
        ),
        claim_pending_wait_seconds=float(
            PreparedSandboxPolicy().claim_pending_wait_seconds
            if claim_pending_wait_seconds is None
            else claim_pending_wait_seconds
        ),
        min_assignment_ttl_seconds=float(
            PreparedSandboxPolicy().min_assignment_ttl_seconds
            if min_assignment_ttl_seconds is None
            else min_assignment_ttl_seconds
        ),
        assignment_ttl_margin_seconds=float(
            PreparedSandboxPolicy().assignment_ttl_margin_seconds
            if assignment_ttl_margin_seconds is None
            else assignment_ttl_margin_seconds
        ),
    )


def prepared_policy_override_from_args(args: argparse.Namespace) -> PreparedSandboxPolicy | None:
    """Return a policy only when CLI flags explicitly enable preparation.

    Args:
        args: Parsed command-line namespace containing prepare flags.

    Returns:
        Policy override when ``--prepare-sandboxes`` was provided; otherwise
        ``None`` so persisted/spec policy remains untouched.
    """

    if not bool(getattr(args, "prepare_sandboxes", False)):
        return None
    return prepared_policy_from_args(args)
