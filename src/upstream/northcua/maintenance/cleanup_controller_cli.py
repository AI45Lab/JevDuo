"""CLI entrypoint for controller residue cleanup."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from northcua.maintenance.cleanup_controller_residue import cleanup_controller_residue


def build_parser() -> argparse.ArgumentParser:
    """Build the controller-residue cleanup CLI parser.

    Returns:
        Configured argument parser.
    """

    parser = argparse.ArgumentParser(description="Clean stale NorthCUA controller residue for one batch")
    parser.add_argument("--batch-db", required=True, type=Path)
    parser.add_argument("--controller-db", required=True, type=Path)
    parser.add_argument("--batch-id", default="")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--protect-prepared", dest="protect_prepared", action="store_true", default=True)
    parser.add_argument("--no-protect-prepared", dest="protect_prepared", action="store_false")
    parser.add_argument("--include-current-active", action="store_true")
    parser.add_argument("--include-prepared", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--provider", default="e2b")
    parser.add_argument("--skip-provider", action="store_true")
    return parser


def main() -> None:
    """Run the controller-residue cleanup CLI and print its JSON summary."""

    args = build_parser().parse_args()
    result = asyncio.run(
        cleanup_controller_residue(
            batch_db=args.batch_db,
            controller_db=args.controller_db,
            batch_id=args.batch_id,
            output_dir=args.output_dir,
            protect_prepared=args.protect_prepared,
            include_current_active=args.include_current_active,
            include_prepared=args.include_prepared,
            apply=args.apply,
            provider=args.provider,
            skip_provider=args.skip_provider,
        )
    )
    print(json.dumps(result, indent=2, ensure_ascii=False, sort_keys=True))
