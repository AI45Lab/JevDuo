"""Local and remote command helpers for terminal verification capture."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Protocol

from nexau.core.messages import ImageBlock, ToolResultBlock

from northcua.core.enums import ResultStatus
from northcua.core.json_types import JsonObject
from northcua.core.models import CommandExecutionResultRecord, CommandRequestRecord, StepLogRecord
from northcua.core.protocol import CommandRequestPayload
from northcua.transport.ws.result_blocks import RemoteToolDispatchResult, coerce_remote_tool_dispatch_result

RemoteToolDispatcher = Callable[[list[JsonObject]], Awaitable[RemoteToolDispatchResult | list[ToolResultBlock]]]


class CommandExecutorProtocol(Protocol):
    """Subset of the command executor used by verification capture."""

    async def execute_command_request(self, payload: CommandRequestPayload) -> object:
        """Execute a command request payload.

        Args:
            payload: Command request containing one verification probe.

        Returns:
            Command execution object exposing a ``results`` sequence.
        """


async def capture_local_image_url(
    *,
    command_executor: CommandExecutorProtocol,
    step_log: StepLogRecord,
    all_screens: bool,
) -> str:
    """Capture a verification image through the local command executor.

    Args:
        command_executor: In-process command executor for local runtime mode.
        step_log: Terminal step being verified.
        all_screens: Whether the desktop screenshot should include all screens.

    Returns:
        Data URL or local path for the captured verification image.

    Raises:
        RuntimeError: If the command executor returns no successful image.
    """

    command = _capture_command(step_log=step_log, all_screens=all_screens)
    command_result = await command_executor.execute_command_request(
        CommandRequestPayload(
            session_id=step_log.session_id,
            round_num=step_log.round_num,
            round_step=step_log.round_step,
            commands=[command],
        )
    )
    results = list(getattr(command_result, "results", []) or [])
    if not results:
        raise RuntimeError("Verification capture returned no command result.")
    result = results[0]
    if result.status != ResultStatus.SUCCESS:
        raise RuntimeError(result.error or "Verification capture command failed.")
    return _image_url_from_command_result(result)


async def capture_remote_image_url(
    *,
    step_log: StepLogRecord,
    all_screens: bool,
    remote_tool_dispatcher: RemoteToolDispatcher | None,
) -> str:
    """Capture a verification image through a remote device dispatcher.

    Args:
        step_log: Terminal step being verified.
        all_screens: Whether the desktop screenshot should include all screens.
        remote_tool_dispatcher: Remote dispatcher bound to the leased device.

    Returns:
        Data URL or remote-local path for the captured verification image.

    Raises:
        RuntimeError: If no dispatcher is available or no image is returned.
    """

    if remote_tool_dispatcher is None:
        raise RuntimeError("Remote verification capture requires a remote tool dispatcher.")
    tool_call = {
        "id": f"verify-{step_log.session_step}",
        "name": "capture_desktop_screenshot",
        "input": {"all_screens": all_screens},
    }
    dispatch_result = coerce_remote_tool_dispatch_result(await remote_tool_dispatcher([tool_call]))
    results = dispatch_result.tool_blocks
    if not results:
        raise RuntimeError("Remote verification capture returned no result.")
    result = results[0]
    if getattr(result, "is_error", False):
        raise RuntimeError("Remote verification capture command failed.")
    return _image_url_from_tool_result_block(result)


def _capture_command(*, step_log: StepLogRecord, all_screens: bool) -> CommandRequestRecord:
    return CommandRequestRecord(
        call_id=f"verify-{step_log.session_step}",
        tool_name="capture_desktop_screenshot",
        server_name="DesktopNativeDataExecutor",
        parameters={"all_screens": all_screens},
        tool_type="probe",
    )


def _image_url_from_command_result(result: CommandExecutionResultRecord) -> str:
    payload = result.result
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict):
        value = payload.get("image_url") or payload.get("path") or payload.get("url")
        if isinstance(value, str):
            return value
    raise RuntimeError("Verification command result did not contain an image URL.")


def _image_url_from_tool_result_block(block: ToolResultBlock) -> str:
    for item in block.content:
        if isinstance(item, ImageBlock) and item.url is not None:
            return item.url
        if isinstance(item, dict):
            value = item.get("image_url") or item.get("url") or item.get("path")
            if isinstance(value, str):
                return value
    raise RuntimeError("Verification remote tool result did not contain an image.")
