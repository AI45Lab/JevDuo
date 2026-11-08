"""CLI parser construction and adapter resolution."""

from __future__ import annotations

import argparse
import logging
from typing import TYPE_CHECKING

from .batch_commands import register_batch_commands, register_sandbox_commands
from .case_commands import register_case_commands
from .controller_commands import register_controller_commands
from .experiment_commands import register_experiment_commands
from .provider_commands import register_provider_probe_command
from .config_commands import register_config_commands
from .demo_commands import register_demo_commands
from .shared_args import default_app_config_path

if TYPE_CHECKING:
    from northcua.agentinfra.runtime.cli import AgentCliAdapterBase

logger = logging.getLogger(__name__)


def cli_adapters() -> tuple["AgentCliAdapterBase", ...]:
    """Instantiate generated adapters for enabled AgentKit agents.

    Returns:
        CLI adapters for enabled agent definitions.

    Raises:
        ModuleNotFoundError: If an optional AgentKit dependency other than
            NexAU is missing and should not be silently skipped.
    """

    try:
        from northcua.agentkit.cli import GeneratedAgentCliAdapter
        from northcua.agentkit.registry import load_all_agent_specs
    except ModuleNotFoundError as exc:
        if exc.name == "nexau":
            logger.debug("AgentKit CLI adapters skipped because NexAU is not installed")
            return ()
        raise
    return tuple(GeneratedAgentCliAdapter(spec) for spec in load_all_agent_specs().enabled())


def resolve_cli_adapter(agent_name: str) -> "AgentCliAdapterBase | None":
    """Resolve the CLI adapter for a parsed command.

    Args:
        agent_name: Parsed CLI agent namespace.

    Returns:
        Matching CLI adapter, or None when no adapter owns the namespace.
    """

    for adapter in cli_adapters():
        if adapter.agent_name == agent_name:
            return adapter
    return None


def build_parser() -> argparse.ArgumentParser:
    """Build the NorthCUA CLI parser.

    Returns:
        Configured argparse parser.
    """

    parser = argparse.ArgumentParser(description="NorthCUA command-line interface")
    subparsers = parser.add_subparsers(dest="command_group", required=True)
    default_config_help = str(default_app_config_path())
    local = subparsers.add_parser("local", help="Run local NorthCUA agents")
    local_subparsers = local.add_subparsers(dest="agent_kind", required=True)
    submit = subparsers.add_parser("submit", help="Submit tasks to the remote controller")
    submit_subparsers = submit.add_subparsers(dest="agent_kind", required=True)
    for adapter in cli_adapters():
        adapter.register_local_commands(local_subparsers, default_config_help=default_config_help)
        adapter.register_submit_commands(submit_subparsers, default_config_help=default_config_help)
    register_config_commands(subparsers)
    register_demo_commands(subparsers)
    register_batch_commands(subparsers)
    register_sandbox_commands(subparsers)
    register_case_commands(subparsers)
    register_experiment_commands(subparsers)
    register_controller_commands(subparsers)
    register_provider_probe_command(subparsers)
    return parser
