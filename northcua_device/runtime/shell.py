"""Shell and fake runtime adapters for the standalone device worker."""

from __future__ import annotations

import asyncio

from northcua_protocol.capabilities import Capability, ToolDescriptor
from northcua_protocol.payloads import CommandRequest, CommandResult
from northcua_protocol.state import CommandState

from northcua_runtime_shared import RuntimeHealth
from northcua_runtime_shared.tool_contracts import ToolContract
from northcua_runtime_shared.device_shell_contracts import (
    DEVICE_ECHO_CONTRACT,
    SHELL_EXEC_CONTRACT,
    SHELL_EXEC_READONLY_CONTRACT,
)
from northcua_runtime_shared.shell_readonly import readonly_shell_argv


class EchoRuntimeAdapter:
    """Safe fake runtime used by tests and connectivity checks."""

    @property
    def capabilities(self) -> set[Capability]:
        """Return capabilities exposed by the echo adapter.

        Returns:
            Minimal safe capability used by tests and smoke checks.
        """

        return {Capability.DESKTOP_SCREENSHOT}

    @property
    def tool_contracts(self) -> list[ToolContract]:
        """Return the canonical echo tool contract."""

        return [DEVICE_ECHO_CONTRACT]

    @property
    def tools(self) -> list[ToolDescriptor]:
        """Return the echo tool descriptor.

        Returns:
            Single side-effect-free tool descriptor for connectivity tests.
        """

        return [contract.to_descriptor() for contract in self.tool_contracts]

    async def healthcheck(self) -> RuntimeHealth:
        """Return health for the in-process echo adapter.

        Returns:
            Healthy runtime snapshot.
        """

        return RuntimeHealth(healthy=True, capability_health={Capability.DESKTOP_SCREENSHOT: True})

    async def execute(self, command: CommandRequest) -> CommandResult:
        """Echo command parameters for deterministic worker tests.

        Args:
            command: Controller-issued command request.

        Returns:
            Successful command result containing the request parameters.
        """

        return CommandResult(
            command_id=command.command_id,
            session_id=command.session_id,
            lease_id=command.lease_id,
            device_id=command.device_id,
            lease_generation=command.lease_generation,
            status=CommandState.SUCCEEDED,
            result={"echo": command.parameters},
        )


class ShellRuntimeAdapter:
    """Optional shell adapter; disabled unless explicitly allowlisted."""

    @property
    def capabilities(self) -> set[Capability]:
        """Return capabilities exposed by the shell adapter.

        Returns:
            Shell execution capability.
        """

        return {Capability.SHELL_EXEC}

    @property
    def tool_contracts(self) -> list[ToolContract]:
        """Return canonical shell tool contracts."""

        return [SHELL_EXEC_CONTRACT, SHELL_EXEC_READONLY_CONTRACT]

    @property
    def tools(self) -> list[ToolDescriptor]:
        """Return shell tool descriptors.

        Returns:
            Descriptor for the explicitly allowlisted ``shell.exec`` tool.
        """

        return [contract.to_descriptor() for contract in self.tool_contracts]

    async def healthcheck(self) -> RuntimeHealth:
        """Return health for local shell execution.

        Returns:
            Healthy runtime snapshot when the adapter is enabled.
        """

        return RuntimeHealth(healthy=True, capability_health={Capability.SHELL_EXEC: True})

    async def execute(self, command: CommandRequest) -> CommandResult:
        """Execute one allowlisted shell command.

        Args:
            command: Controller-issued shell command request.

        Returns:
            Command result with return code, stdout, and stderr.
        """

        cmd = str(command.parameters.get("cmd") or "")
        if command.tool_id == "shell.exec_readonly":
            argv = readonly_shell_argv(cmd)
            process = await asyncio.create_subprocess_exec(
                *argv,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        else:
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=command.timeout_seconds)
        status = CommandState.SUCCEEDED if process.returncode == 0 else CommandState.FAILED
        return CommandResult(
            command_id=command.command_id,
            session_id=command.session_id,
            lease_id=command.lease_id,
            device_id=command.device_id,
            lease_generation=command.lease_generation,
            status=status,
            result={
                "returncode": process.returncode,
                "stdout": stdout.decode("utf-8", errors="replace"),
                "stderr": stderr.decode("utf-8", errors="replace"),
            },
            error=None if status == CommandState.SUCCEEDED else "shell command failed",
        )
