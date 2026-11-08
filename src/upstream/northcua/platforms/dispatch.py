"""Typed helpers for routing tool/action calls through platform runtimes."""

from __future__ import annotations

from northcua.core.enums import ResultStatus
from northcua.core.models import CommandExecutionResultRecord
from northcua.core.protocol import CommandRequestPayload, CommandResultPayload
from northcua.platforms.protocol import (
    PlatformActionRequest,
    PlatformActionResult,
    PlatformActionStatus,
    PlatformRuntime,
)


class PlatformActionDispatcher:
    """Dispatch normalized platform action requests through a runtime."""

    def __init__(self, runtime: PlatformRuntime) -> None:
        """Initialize the dispatcher.

        Args:
            runtime: Platform runtime used to execute actions.
        """

        self.runtime = runtime

    async def execute(self, request: PlatformActionRequest) -> PlatformActionResult:
        """Execute one platform action request.

        Args:
            request: Normalized platform action request.

        Returns:
            Platform action result.
        """

        return await self.runtime.execute_action(request)

    @staticmethod
    def from_command_result(record: CommandExecutionResultRecord) -> PlatformActionResult:
        """Project a command result record into a platform action result.

        Args:
            record: Command result from a runtime/MCP executor.

        Returns:
            Equivalent platform action result.
        """

        status = PlatformActionStatus.SUCCESS if record.status == ResultStatus.SUCCESS else PlatformActionStatus.FAILURE
        return PlatformActionResult(
            call_id=record.call_id,
            status=status,
            result=record.result,
            error=record.error,
        )


class PlatformCommandExecutor:
    """Command-request adapter that routes actions through a platform runtime."""

    def __init__(self, runtime: PlatformRuntime) -> None:
        """Initialize the command executor.

        Args:
            runtime: Platform runtime that executes normalized actions.
        """

        self.dispatcher = PlatformActionDispatcher(runtime)

    async def execute_command_request(self, payload: CommandRequestPayload) -> CommandResultPayload:
        """Execute a command batch through the platform runtime contract.

        Args:
            payload: Command request payload.

        Returns:
            Command result payload preserving command call IDs.
        """

        results = [
            self._command_result_from_platform(
                await self.dispatcher.execute(
                    PlatformActionRequest(
                        name=command.tool_name,
                        parameters=command.parameters,
                        call_id=command.call_id,
                        server_name=command.server_name,
                        metadata={
                            "session_id": payload.session_id,
                            "round_num": payload.round_num,
                            "round_step": payload.round_step,
                            "tool_type": command.tool_type,
                        },
                    )
                )
            )
            for command in payload.commands
        ]
        status = (
            ResultStatus.FAILURE
            if any(result.status == ResultStatus.FAILURE for result in results)
            else ResultStatus.SUCCESS
        )
        return CommandResultPayload(session_id=payload.session_id, status=status, results=results)

    @staticmethod
    def _command_result_from_platform(result: PlatformActionResult) -> CommandExecutionResultRecord:
        status = ResultStatus.SUCCESS if result.status == PlatformActionStatus.SUCCESS else ResultStatus.FAILURE
        return CommandExecutionResultRecord(
            call_id=result.call_id,
            status=status,
            result=result.result,
            error=result.error,
        )
