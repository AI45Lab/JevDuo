"""CLI launcher that exposes registered local FastMCP servers over stdio."""

from __future__ import annotations

import argparse
import logging

from northcua.runtime.mcp.agent_extensions import run_agent_mcp_registration_hook
from northcua.runtime.mcp.builtin_servers import ensure_builtin_mcp_servers_registered
from northcua.runtime.mcp.registry import MCPFactoryContext, NorthCUAMCPRegistry

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    """Build the stdio launcher CLI.

    Returns:
        Argument parser for launching a registered MCP server.
    """

    parser = argparse.ArgumentParser(description="Run a registered NorthCUA MCP server over stdio.")
    parser.add_argument("server_name", help="Registry name of the MCP server to start.")
    parser.add_argument("--process-name", default="", help="Optional process name hint passed to the server factory.")
    parser.add_argument(
        "--registration-hook",
        default="",
        help="Optional module:function hook that registers agent-owned MCP servers.",
    )
    return parser


def main() -> None:
    """Entrypoint used by NexAU stdio MCP configs."""

    args = build_parser().parse_args()
    ensure_builtin_mcp_servers_registered()
    if args.registration_hook:
        run_agent_mcp_registration_hook(args.registration_hook)
    if not NorthCUAMCPRegistry.is_registered(args.server_name):
        logger.debug("No built-in MCP server registered for '%s' before explicit build.", args.server_name)
    server = NorthCUAMCPRegistry.build(
        args.server_name,
        context=MCPFactoryContext(process_name=args.process_name),
    )
    logger.info("Starting MCP server '%s' over stdio", args.server_name)
    server.run(transport="stdio", show_banner=False)


if __name__ == "__main__":
    main()
