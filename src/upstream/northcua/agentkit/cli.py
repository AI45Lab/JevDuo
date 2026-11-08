"""Generated CLI adapter for AgentKit specs."""

from __future__ import annotations

import argparse
import inspect

from northcua.agentinfra.runtime.cli import AgentCliAdapterBase

from .imports import resolve_dotted
from .spec import AgentCliCommandSpec, AgentSpec, AsyncCliHandler


class GeneratedAgentCliAdapter(AgentCliAdapterBase):
    """CLI adapter generated from an ``AgentSpec``."""

    def __init__(self, spec: AgentSpec) -> None:
        self.spec = spec
        self._commands_by_name: dict[str, AgentCliCommandSpec] = {}
        for command in spec.cli.local_commands:
            self._commands_by_name[f"local:{command.name}"] = command
        if spec.cli.submit_command is not None:
            self._commands_by_name["submit:__submit__"] = spec.cli.submit_command

    @property
    def agent_name(self) -> str:
        """Return the generated CLI namespace.

        Returns:
            Agent name used under ``northcua local`` and ``northcua submit``.
        """

        return self.spec.cli.agent_name

    def register_local_commands(
        self,
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
        *,
        default_config_help: str,
    ) -> None:
        """Register generated local commands.

        Args:
            subparsers: Parent ``local`` subparser collection to extend.
            default_config_help: Legacy config help text; generated commands
                carry their own argument metadata.
        """

        del default_config_help
        agent_parser = subparsers.add_parser(self.spec.cli.agent_name, help=self.spec.cli.help)
        local_subparsers = agent_parser.add_subparsers(dest="agent_action", required=True)
        for command in self.spec.cli.local_commands:
            if not command.enabled:
                continue
            parser = local_subparsers.add_parser(command.name, help=command.help)
            for argument in command.arguments:
                argument.add_to(parser)

    def register_submit_commands(
        self,
        subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
        *,
        default_config_help: str,
    ) -> None:
        """Register generated submit command.

        Args:
            subparsers: Parent ``submit`` subparser collection to extend.
            default_config_help: Legacy config help text; generated commands
                carry their own argument metadata.
        """

        del default_config_help
        command = self.spec.cli.submit_command
        if command is None or not command.enabled:
            return
        parser = subparsers.add_parser(self.spec.cli.agent_name, help=command.help)
        for argument in command.arguments:
            argument.add_to(parser)

    async def dispatch(self, args: argparse.Namespace) -> int:
        """Dispatch a generated command.

        Args:
            args: Parsed CLI namespace from the top-level NorthCUA parser.

        Returns:
            Process-style exit code returned by the command handler.

        Raises:
            ValueError: If no generated command matches the parsed namespace.
        """

        if args.command_group == "local":
            command = self._commands_by_name.get(f"local:{args.agent_action}")
        elif args.command_group == "submit":
            command = self._commands_by_name.get("submit:__submit__")
        else:
            command = None
        if command is None:
            raise ValueError(f"Unsupported {self.spec.kind} command group: {args.command_group}")
        handler = resolve_dotted(command.handler)
        return await _call_handler(handler, args)


async def _call_handler(handler: AsyncCliHandler, args: argparse.Namespace) -> int:
    """Call a handler with args projected by parameter name.

    Args:
        handler: Async command handler declared by the agent spec.
        args: Parsed CLI namespace containing generated command fields.

    Returns:
        Process-style exit code returned by the handler.
    """

    signature = inspect.signature(handler)
    kwargs = {
        name: getattr(args, name)
        for name, parameter in signature.parameters.items()
        if name != "args"
        and hasattr(args, name)
        and parameter.kind in {parameter.POSITIONAL_OR_KEYWORD, parameter.KEYWORD_ONLY}
    }
    if "args" in signature.parameters:
        return await handler(args)
    return await handler(**kwargs)  # type: ignore[arg-type]
