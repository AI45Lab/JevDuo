"""Controller-side bridge that executes AgentKit tool batches on a leased device."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping

from northcua.agentkit.controller_dispatch import (
    command_result_record,
    failed_command_result_record,
    tool_error_command_result_record,
)
from northcua.agentkit.runtime_sidecars import split_runtime_sidecar
from northcua.agentkit.session_context import AGENTKIT_SETTINGS_PARAM, sanitize_agentkit_settings_side_channel
from northcua.controller.execution.remote_dispatcher import RemoteCommandDispatcher
from northcua.core.enums import ResultStatus
from northcua.core.json_types import JsonObject
from northcua.core.protocol import CommandRequestPayload, CommandResultPayload
from northcua_runtime_shared.validation import ToolParameterValidationError


logger = logging.getLogger(__name__)


class RemoteAgentKitDeviceCommandExecutor:
    """Execute AgentKit MCP commands inside the leased device worker.

    The local ``NorthCUADeviceCommandExecutor`` runs MCP servers in the current
    process. That is correct for local desktop/web runs, but remote browser
    actions must run in the process that owns the browser. This adapter keeps
    the same ``execute_command_request`` shape while dispatching each command
    through the controller/device command channel.
    """

    def __init__(
        self,
        dispatcher: RemoteCommandDispatcher,
        *,
        namespace: str,
        timeout_seconds: float,
        side_channel_settings: Mapping[str, object] | None = None,
        dynamic_side_channel_settings: Callable[[], Mapping[str, object]] | None = None,
        runtime_sidecar_handler: Callable[[Mapping[str, object]], None] | None = None,
        parameter_validation_error_projector: Callable[[str, ToolParameterValidationError], JsonObject | None]
        | None = None,
    ) -> None:
        self.dispatcher = dispatcher
        self.namespace = namespace.strip(".")
        self.timeout_seconds = timeout_seconds
        self.side_channel_settings = sanitize_agentkit_settings_side_channel(dict(side_channel_settings or {}))
        self.dynamic_side_channel_settings = dynamic_side_channel_settings
        self.runtime_sidecar_handler = runtime_sidecar_handler
        self.parameter_validation_error_projector = parameter_validation_error_projector

    async def execute_command_request(self, payload: CommandRequestPayload) -> CommandResultPayload:
        """Dispatch all commands to the remote worker and return local runtime records."""

        results = []
        for command in payload.commands:
            parameters: JsonObject = dict(command.parameters)
            side_channel_settings = self._side_channel_settings()
            if side_channel_settings:
                existing_settings = parameters.get(AGENTKIT_SETTINGS_PARAM)
                merged_settings: JsonObject = {}
                if isinstance(existing_settings, Mapping):
                    merged_settings.update(sanitize_agentkit_settings_side_channel(dict(existing_settings)))
                merged_settings.update(side_channel_settings)
                parameters[AGENTKIT_SETTINGS_PARAM] = sanitize_agentkit_settings_side_channel(merged_settings)
            try:
                result = await self.dispatcher.dispatch(
                    tool_id=self._tool_id(command.tool_name),
                    parameters=parameters,
                    command_id=command.call_id,
                    timeout_seconds=self.timeout_seconds,
                )
            except ToolParameterValidationError as exc:
                logger.info(
                    "agentkit_remote_tool_parameters_rejected",
                    extra={
                        "tool_name": command.tool_name,
                        "call_id": command.call_id,
                        "parameter_keys": sorted(command.parameters),
                        "error": str(exc),
                    },
                )
                projected = (
                    self.parameter_validation_error_projector(command.tool_name, exc)
                    if self.parameter_validation_error_projector is not None
                    else None
                )
                results.append(
                    tool_error_command_result_record(call_id=command.call_id, payload=projected)
                    if projected is not None
                    else failed_command_result_record(call_id=command.call_id, error=str(exc))
                )
                continue
            result = self._consume_runtime_sidecar(result)
            results.append(command_result_record(command.call_id, result))
        status = (
            ResultStatus.FAILURE
            if any(result.status == ResultStatus.FAILURE for result in results)
            else ResultStatus.SUCCESS
        )
        return CommandResultPayload(
            session_id=payload.session_id,
            status=status,
            results=results,
        )

    def _tool_id(self, tool_name: str) -> str:
        if "." in tool_name:
            return tool_name
        return f"{self.namespace}.{tool_name}"

    def _side_channel_settings(self) -> JsonObject:
        """Return static settings plus per-command runtime additions."""

        settings: JsonObject = dict(self.side_channel_settings)
        if self.dynamic_side_channel_settings is not None:
            settings.update(dict(self.dynamic_side_channel_settings()))
        return sanitize_agentkit_settings_side_channel(settings) if settings else {}

    def _consume_runtime_sidecar(self, result):
        payload, sidecar = split_runtime_sidecar(result.result)
        if sidecar and self.runtime_sidecar_handler is not None:
            self.runtime_sidecar_handler(sidecar)
        if payload is result.result:
            return result
        return result.model_copy(update={"result": payload})
