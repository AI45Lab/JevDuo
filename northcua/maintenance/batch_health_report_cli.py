"""CLI entrypoint for the batch health report command."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from northcua.maintenance.batch_health_report import generate_batch_health_report


def build_parser() -> argparse.ArgumentParser:
    """Build the health-report CLI parser.

    Returns:
        Configured argument parser.
    """

    parser = argparse.ArgumentParser(description="Generate a unified NorthCUA batch health report")
    parser.add_argument("--batch-db", required=True, type=Path)
    parser.add_argument("--controller-db", type=Path, default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--restart-time", default="")
    parser.add_argument("--cloudflared-metrics-url", default="")
    parser.add_argument("--socket-port", type=int, default=9192)
    parser.add_argument("--recent-window-minutes", type=int, default=20)
    parser.add_argument("--json", action="store_true", help="Print JSON")
    parser.add_argument("--markdown", action="store_true", help="Print Markdown")
    return parser


def main() -> None:
    """Run the health-report CLI and print Markdown or JSON output."""

    args = build_parser().parse_args()
    report = generate_batch_health_report(
        batch_db=args.batch_db,
        controller_db=args.controller_db,
        run_dir=args.run_dir,
        restart_time=args.restart_time,
        cloudflared_metrics_url=args.cloudflared_metrics_url,
        socket_port=args.socket_port,
        recent_window_minutes=args.recent_window_minutes,
    )
    if args.markdown and not args.json:
        print(report["markdown"])
        return
    print(json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True))
