"""Remote direct-tool proxy implementations for websocket execution."""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Protocol

from nexau.core.messages import ImageBlock, TextBlock, ToolResultBlock

from northcua.app.logging import log_runtime_event
from northcua.core.enums import ResultStatus
from northcua.core.json_types import JsonObject
from northcua.core.models import CommandExecutionResultRecord
from northcua.transport.ws.result_blocks import RemoteToolDispatchResult, coerce_remote_tool_dispatch_result

logger = logging.getLogger(__name__)
_IMAGE_DATA_URL_RE = re.compile(r"data:image/\S+", re.IGNORECASE)
_BASE64_IMAGE_DATA_URL_RE = re.compile(
    r"^data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+$",
    re.IGNORECASE,
)
_MATERIALIZED_IMAGE_URL_KEYS = {
    "screenshot_url",
    "image_url",
    "raw_image_url",
    "action_overlay_image_url",
    "som_image_url",
}

ExternalToolDispatcher = Callable[[list[JsonObject]], Awaitable[RemoteToolDispatchResult | list[ToolResultBlock]]]


class RemoteToolArtifactSink(Protocol):
    """Materialize remote inline artifacts into the active runner artifact store."""

    def materialize_image_data_url(
        self,
        *,
        tool_name: str,
        key: str,
        data_url: str,
        metadata: JsonObject,
    ) -> str:
        """Persist one image data URL and return a local runner-visible path."""

        ...


class RemoteCommandToolExecutor:
    """Execute model-visible tools by dispatching commands to a remote device."""

    def __init__(
        self,
        dispatch_external_calls: ExternalToolDispatcher,
        *,
        artifact_sink: RemoteToolArtifactSink | None = None,
    ) -> None:
        """Initialize the executor.

        Args:
            dispatch_external_calls: Transport callback that sends command-shaped
                tool calls to the leased device and waits for command results.
            artifact_sink: Optional runner-owned sink for remote inline images.
        """

        self._dispatch_external_calls = dispatch_external_calls
        self._artifact_sink = artifact_sink

    async def execute(self, *, tool_name: str, parameters: JsonObject, server_name: str = "") -> JsonObject:
        """Execute one remote tool through the websocket command data plane.

        Args:
            tool_name: Model-visible tool name selected by the LLM.
            parameters: JSON-like tool parameters after NexAU validation and
                before-tool middleware have run.
            server_name: Owning MCP server from the resolved ToolContract route.

        Returns:
            JSON-like raw tool output. NexAU will run its normal formatter and
            after-tool hooks around this value, matching local execution.
        """

        call_id = f"remote-proxy-{uuid.uuid4().hex}"
        log_runtime_event(
            logger,
            "transport_remote_tool_proxy_dispatch_started",
            tool_name=tool_name,
            server_name=server_name,
            proxy_call_id=call_id,
            parameter_keys=sorted(parameters),
        )
        dispatch_result = coerce_remote_tool_dispatch_result(
            await self._dispatch_external_calls(
                [
                    {
                        "id": call_id,
                        "name": tool_name,
                        "server_name": server_name,
                        "input": dict(parameters),
                    }
                ]
            )
        )
        command_result = self._matching_command_result(dispatch_result, call_id)
        if command_result is not None:
            log_runtime_event(
                logger,
                "transport_remote_tool_proxy_dispatch_completed",
                tool_name=tool_name,
                server_name=server_name,
                proxy_call_id=call_id,
                status=command_result.status,
                has_result=command_result.result is not None,
            )
            if command_result.result is None:
                if command_result.error:
                    return {"status": "error", "error": command_result.error}
                return {}
            if isinstance(command_result.result, dict):
                payload: JsonObject = {str(key): value for key, value in command_result.result.items()}
            else:
                payload = {"result": command_result.result}
            payload = _materialize_or_strip_inline_image_urls(
                payload,
                artifact_sink=self._artifact_sink,
                tool_name=tool_name,
            )
            if command_result.status == ResultStatus.FAILURE:
                payload.setdefault("status", "error")
                if command_result.error:
                    payload.setdefault("error", command_result.error)
            return payload

        if not dispatch_result.tool_blocks:
            log_runtime_event(
                logger,
                "transport_remote_tool_proxy_no_result",
                level=logging.WARNING,
                tool_name=tool_name,
                proxy_call_id=call_id,
            )
            return {"status": "error", "error": "Remote command dispatcher returned no result."}
        fallback = self._tool_result_block_to_output(dispatch_result.tool_blocks[0])
        log_runtime_event(
            logger,
            "transport_remote_tool_proxy_dispatch_completed_without_raw_command",
            level=logging.DEBUG,
            tool_name=tool_name,
            proxy_call_id=call_id,
            fallback_keys=sorted(fallback),
        )
        return fallback

    @staticmethod
    def _matching_command_result(
        dispatch_result: RemoteToolDispatchResult,
        call_id: str,
    ) -> CommandExecutionResultRecord | None:
        """Return the raw command result attached to a dispatcher batch."""

        for result in dispatch_result.command_results:
            if result.call_id == call_id or result.call_id.endswith(f"::{call_id}"):
                return result
        return None

    @staticmethod
    def _tool_result_block_to_output(block: ToolResultBlock) -> JsonObject:
        """Convert a provider-facing tool result block into safe raw output.

        This is a defensive fallback for dispatchers that only return
        provider-facing blocks and forget to attach raw command rows. Image
        blocks can contain large base64 data URLs; returning them as JSON would
        make the next model call treat the screenshot as text and explode the
        prompt size. The normal path should recover image metadata from the raw
        ``CommandExecutionResultRecord`` instead.
        """

        if isinstance(block.content, str):
            text, removed = _strip_embedded_image_data_urls(block.content)
            payload: JsonObject = {"text": text}
            if removed:
                payload["image_omitted"] = True
                payload["image_omitted_reason"] = "embedded_inline_image_url_removed_from_remote_tool_text"
        else:
            texts: list[str] = []
            omitted_images = 0
            for item in block.content:
                if isinstance(item, TextBlock):
                    text, removed = _strip_embedded_image_data_urls(item.text)
                    texts.append(text)
                    if removed:
                        omitted_images += 1
                elif isinstance(item, ImageBlock):
                    omitted_images += 1
            payload = {}
            if texts:
                payload["text"] = "\n".join(texts)
            if omitted_images:
                payload["image_omitted"] = True
                payload["image_omitted_reason"] = "remote_tool_result_block_without_raw_command"
        if block.is_error:
            payload["status"] = "error"
        return payload


_CURRENT_REMOTE_EXECUTOR: contextvars.ContextVar[RemoteCommandToolExecutor | None] = contextvars.ContextVar(
    "northcua_remote_command_tool_executor",
    default=None,
)
_CURRENT_REMOTE_ARTIFACT_SINK: contextvars.ContextVar[RemoteToolArtifactSink | None] = contextvars.ContextVar(
    "northcua_remote_tool_artifact_sink",
    default=None,
)


class RemoteToolProxyFactory:
    """Create NexAU direct-tool implementations that proxy to the current device."""

    def build(self, *, tool_name: str, server_name: str = "") -> Callable[..., Awaitable[JsonObject]]:
        """Create one async implementation for a model-visible remote tool.

        Args:
            tool_name: Tool name that should be dispatched to the remote device.
            server_name: Owning MCP server from the resolved contract route.

        Returns:
            Async function suitable for ``nexau.archs.tool.Tool``.
        """

        async def _implementation(**params: object) -> JsonObject:
            executor = _CURRENT_REMOTE_EXECUTOR.get()
            if executor is None:
                return {
                    "status": "error",
                    "error": "Remote tool proxy is not bound to an active dispatcher.",
                    "tool_name": tool_name,
                    "server_name": server_name,
                }
            return await executor.execute(
                tool_name=tool_name,
                server_name=server_name,
                parameters={str(key): value for key, value in params.items()},
            )

        return _implementation


@contextlib.asynccontextmanager
async def use_remote_command_tool_executor(
    dispatch_external_calls: ExternalToolDispatcher,
    *,
    artifact_sink: RemoteToolArtifactSink | None = None,
) -> AsyncIterator[RemoteCommandToolExecutor]:
    """Bind a remote command executor for the current NexAU model round.

    Args:
        dispatch_external_calls: Transport callback used by proxy tools.
        artifact_sink: Optional sink for runner-local image artifact paths.

    Returns:
        Async context manager that yields the active remote command tool
        executor for proxy implementations.

    Yields:
        Active remote command tool executor.
    """

    executor = RemoteCommandToolExecutor(
        dispatch_external_calls,
        artifact_sink=artifact_sink or _CURRENT_REMOTE_ARTIFACT_SINK.get(),
    )
    token = _CURRENT_REMOTE_EXECUTOR.set(executor)
    try:
        yield executor
    finally:
        _CURRENT_REMOTE_EXECUTOR.reset(token)


@contextlib.contextmanager
def use_remote_tool_artifact_sink(
    artifact_sink: RemoteToolArtifactSink | None,
) -> object:
    """Bind a runner-owned remote artifact sink for nested proxy execution."""

    token = _CURRENT_REMOTE_ARTIFACT_SINK.set(artifact_sink)
    try:
        yield artifact_sink
    finally:
        _CURRENT_REMOTE_ARTIFACT_SINK.reset(token)


__all__ = [
    "ExternalToolDispatcher",
    "RemoteCommandToolExecutor",
    "RemoteToolArtifactSink",
    "RemoteToolProxyFactory",
    "use_remote_command_tool_executor",
    "use_remote_tool_artifact_sink",
]


def _materialize_or_strip_inline_image_urls(
    payload: JsonObject,
    *,
    artifact_sink: RemoteToolArtifactSink | None,
    tool_name: str,
) -> JsonObject:
    """Materialize or remove inline image URLs before model history capture."""

    sanitized, materialized, removed = _sanitize_inline_image_urls(
        dict(payload),
        artifact_sink=artifact_sink,
        tool_name=tool_name,
        metadata=dict(payload),
    )
    stripped = sanitized if isinstance(sanitized, dict) else {"result": sanitized}
    if materialized:
        stripped["image_materialized"] = True
        stripped["image_materialized_reason"] = "remote_inline_image_persisted_to_runner_artifact_store"
    elif removed:
        stripped["image_omitted"] = True
        stripped["image_omitted_reason"] = "inline_image_url_removed_from_remote_tool_output"
    return stripped


def _sanitize_inline_image_urls(
    value: object,
    *,
    artifact_sink: RemoteToolArtifactSink | None,
    tool_name: str,
    metadata: JsonObject,
    key_hint: str = "",
) -> tuple[object, bool, bool]:
    """Recursively strip or materialize inline image data URLs."""

    if isinstance(value, str):
        if not _is_image_data_url(value):
            sanitized, removed = _strip_embedded_image_data_urls(value)
            return sanitized, False, removed
        if not _is_base64_image_data_url(value):
            return "[image data omitted]", False, True
        path = _materialize_inline_image_url(
            artifact_sink=artifact_sink,
            tool_name=tool_name,
            key=key_hint or "image",
            data_url=value,
            metadata=metadata,
        )
        if path and key_hint in _MATERIALIZED_IMAGE_URL_KEYS:
            return path, True, False
        return "[image data omitted; materialized]" if path else "[image data omitted]", bool(path), not path
    if isinstance(value, list):
        items: list[object] = []
        materialized = False
        removed = False
        for item in value:
            sanitized, item_materialized, item_removed = _sanitize_inline_image_urls(
                item,
                artifact_sink=artifact_sink,
                tool_name=tool_name,
                metadata=metadata,
                key_hint=key_hint,
            )
            items.append(sanitized)
            materialized = materialized or item_materialized
            removed = removed or item_removed
        return items, materialized, removed
    if isinstance(value, dict):
        output: JsonObject = {}
        materialized = False
        removed = False
        for key, item in value.items():
            key_text = str(key)
            output_key, key_removed = _strip_embedded_image_data_urls(key_text)
            if key_removed:
                removed = True
            if isinstance(item, str) and _is_image_data_url(item) and key_text in _MATERIALIZED_IMAGE_URL_KEYS:
                if _is_base64_image_data_url(item):
                    path = _materialize_inline_image_url(
                        artifact_sink=artifact_sink,
                        tool_name=tool_name,
                        key=key_text,
                        data_url=item,
                        metadata={**metadata, **{str(k): v for k, v in value.items()}},
                    )
                    if path:
                        output[_image_path_key_for_url_key(key_text)] = path
                        materialized = True
                    else:
                        removed = True
                else:
                    removed = True
                continue
            sanitized, item_materialized, item_removed = _sanitize_inline_image_urls(
                item,
                artifact_sink=artifact_sink,
                tool_name=tool_name,
                metadata=metadata,
                key_hint=key_text,
            )
            output[_unique_output_key(output, output_key)] = sanitized
            materialized = materialized or item_materialized
            removed = removed or item_removed
        return output, materialized, removed
    return value, False, False


def _is_image_data_url(value: str) -> bool:
    """Return whether a string is an image data URL, case-insensitively."""

    return value[:11].lower() == "data:image/"


def _is_base64_image_data_url(value: str) -> bool:
    """Return whether a full string is a base64 image data URL we can materialize."""

    return bool(_BASE64_IMAGE_DATA_URL_RE.fullmatch(value))


def _strip_embedded_image_data_urls(value: str) -> tuple[str, bool]:
    """Remove inline image data URLs embedded inside otherwise textual values."""

    stripped = _IMAGE_DATA_URL_RE.sub("[image data omitted]", value)
    return stripped, stripped != value


def _unique_output_key(output: JsonObject, key: str) -> str:
    if key not in output:
        return key
    index = 2
    while f"{key}_{index}" in output:
        index += 1
    return f"{key}_{index}"


def _image_path_key_for_url_key(key: str) -> str:
    if key == "screenshot_url":
        return "screenshot_path"
    if key == "raw_image_url":
        return "raw_image_path"
    if key == "action_overlay_image_url":
        return "action_overlay_image_path"
    if key == "som_image_url":
        return "som_image_path"
    return "image_path"


def _materialize_inline_image_url(
    *,
    artifact_sink: RemoteToolArtifactSink | None,
    tool_name: str,
    key: str,
    data_url: str,
    metadata: JsonObject,
) -> str:
    if artifact_sink is None:
        return ""
    try:
        return artifact_sink.materialize_image_data_url(
            tool_name=tool_name,
            key=key,
            data_url=data_url,
            metadata=metadata,
        )
    except Exception:
        logger.debug(
            "transport_remote_tool_artifact_materialize_failed",
            exc_info=True,
            extra={"tool_name": tool_name, "key": key},
        )
        return ""
