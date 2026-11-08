"""Shared CLI adapter scaffolding for internal agents."""

from __future__ import annotations

import argparse
from abc import ABC, abstractmethod


class AgentCliAdapterBase(ABC):
    """Base class for agent-specific CLI adapters."""

    @property
    @abstractmethod
    def agent_name(self) -> str:
        """Return the stable CLI agent name.

        Returns:
            Agent name used under grouped local and submitter commands.
        """

    @abstractmethod
    def register_local_commands(
        self,
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
        *,
        default_config_help: str,
    ) -> None:
        """Register grouped local CLI commands for the adapter.

        Args:
            subparsers: ``northcua local`` agent subparser collection.
            default_config_help: Help text for the default app config path.
        """

    @abstractmethod
    def register_submit_commands(
        self,
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
        *,
        default_config_help: str,
    ) -> None:
        """Register grouped submitter CLI commands for the adapter.

        Args:
            subparsers: ``northcua submit`` agent subparser collection.
            default_config_help: Help text for the default app config path.
        """

    @abstractmethod
    async def dispatch(self, args: argparse.Namespace) -> int:
        """Dispatch one parsed command.

        Args:
            args: Parsed argparse namespace.

        Returns:
            Process-style exit code.
        """
