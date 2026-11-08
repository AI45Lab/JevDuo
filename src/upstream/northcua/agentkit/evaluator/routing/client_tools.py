"""Client-side evaluator tool dispatch contracts."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from typing import Protocol
from uuid import uuid4

from northcua.agentkernel.json_types import JsonObject
from northcua.core.enums import ResultStatus
from northcua.core.models import CommandExecutionResultRecord
from northcua_runtime_shared.shell_readonly import validate_readonly_shell_command

from ..spec import EvaluatorToolRef

ExternalEvaluatorToolDispatcher = Callable[[list[JsonObject]], Awaitable[object]]
_DATA_IMAGE_URL_RE = re.compile(r"^data:image/", re.IGNORECASE)
_INLINE_DATA_IMAGE_URL_RE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=_-]+", re.IGNORECASE)
_ABSOLUTE_PATH_RE = re.compile(r"(^|[\s'\"({\[=])/(?:[^\s'\"<>),}\]]+)")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s'\"<>]+")
_PATH_RESULT_KEYS = {"path", "screenshot_path", "image_path", "source_screenshot_path", "overlay_screenshot_path"}
_RAW_ERROR_RESULT_KEYS = {"capture_error", "image_omitted_reason"}


class EvaluatorClientToolDispatcher(Protocol):
    """Dispatch evaluator live tools to the evaluated client/device."""

    def dispatch(
        self,
        *,
        tool: EvaluatorToolRef,
        parameters: JsonObject,
        remote: bool,
    ) -> object | Awaitable[object]:
        """Execute one client-side evaluator tool.

        Args:
            tool: Declared evaluator tool spec.
            parameters: Validated model-supplied tool parameters.
            remote: Whether the evaluated target is remote.

        Returns:
            Tool result payload, synchronously or asynchronously.
        """


class LocalEvaluatorClientToolDispatcher:
    """Run local client evaluator tools through their declared implementation."""

    def dispatch(
        self,
        *,
        tool: EvaluatorToolRef,
        parameters: JsonObject,
        remote: bool,
    ) -> object | Awaitable[object]:
        """Execute a local client tool implementation.

        Args:
            tool: Declared evaluator tool spec.
            parameters: Validated model-supplied tool parameters.
            remote: Whether the evaluated target is remote.

        Returns:
            Tool implementation result.

        Raises:
            ValueError: If a local client tool lacks an implementation.
        """

        del remote
        if tool.implementation is None:
            raise ValueError(f"Local evaluator client tool '{tool.name}' has no implementation.")
        return tool.implementation(**parameters)


class UnavailableEvaluatorClientToolDispatcher:
    """Null-object dispatcher for artifact-only evaluator replays.

    Offline replay can load a configuration that normally exposes live client
    probes.  Keeping the declared tool surface lets the model handle the
    capability loss explicitly, while this dispatcher guarantees that replay
    never reaches the current host or a stale remote device by accident.
    """

    def __init__(self, *, reason: str = "Client tools are unavailable during artifact-only replay.") -> None:
        self.reason = reason

    def dispatch(
        self,
        *,
        tool: EvaluatorToolRef,
        parameters: JsonObject,
        remote: bool,
    ) -> JsonObject:
        """Return a deterministic unavailable result without executing I/O."""

        del parameters, remote
        return {
            "status": "failure",
            "tool": tool.name,
            "error": self.reason,
            "error_type": "ClientToolUnavailable",
        }


class RemoteEvaluatorClientToolDispatcher:
    """Dispatch evaluator client tools through the controller/device path.

    This adapter uses the same pending-tool-call shape as the main remote
    desktop agent. The controller/device dispatcher remains the single path
    that actually executes the command against the client runtime.
    """

    def __init__(self, dispatch_external_calls: ExternalEvaluatorToolDispatcher) -> None:
        """Create a dispatcher adapter for controller/device tool execution.

        Args:
            dispatch_external_calls: Async callable that sends pending tool
                calls through the controller's external tool dispatcher.
        """

        self.dispatch_external_calls = dispatch_external_calls

    async def dispatch(
        self,
        *,
        tool: EvaluatorToolRef,
        parameters: JsonObject,
        remote: bool,
    ) -> JsonObject:
        """Dispatch one evaluator live tool to the remote device.

        Args:
            tool: Declared evaluator client tool spec.
            parameters: Model-supplied tool parameters.
            remote: Must be ``True`` for controller/device dispatch.

        Returns:
            JSON object summarizing the remote tool result.

        Raises:
            ValueError: If called for a non-remote target.
        """

        if not remote:
            raise ValueError("RemoteEvaluatorClientToolDispatcher requires remote=True.")
        call_id = f"evaluator-{tool.name}-{uuid4().hex}"
        dispatch_name = tool.dispatch_name or tool.name
        remote_parameters = self._remote_parameters(tool=tool, parameters=parameters, dispatch_name=dispatch_name)
        tool_blocks, command_results = self._coerce_remote_dispatch_result(
            await self.dispatch_external_calls(
                [
                    {
                        "id": call_id,
                        "name": dispatch_name,
                        "server_name": tool.server_name,
                        "input": remote_parameters,
                    }
                ]
            )
        )
        command_result = self._matching_command_result(call_id=call_id, results=command_results)
        if command_result is not None:
            return self._payload_from_command_result(command_result, model_tool_name=tool.name)
        block = tool_blocks[0] if tool_blocks else None
        if block is None:
            return {
                "status": "failure",
                "tool": tool.name,
                "dispatch_name": dispatch_name,
                "error": "No remote result.",
            }
        return self._payload_from_tool_block(block, model_tool_name=tool.name, dispatch_name=dispatch_name)

    @staticmethod
    def _coerce_remote_dispatch_result(value: object) -> tuple[list[object], list[CommandExecutionResultRecord]]:
        if isinstance(value, list):
            return list(value), []
        raw_tool_blocks = getattr(value, "tool_blocks", [])
        raw_command_results = getattr(value, "command_results", [])
        tool_blocks = list(raw_tool_blocks) if isinstance(raw_tool_blocks, list) else []
        command_results = (
            list(raw_command_results)
            if isinstance(raw_command_results, list)
            and all(isinstance(item, CommandExecutionResultRecord) for item in raw_command_results)
            else []
        )
        return tool_blocks, command_results

    @classmethod
    def _remote_parameters(
        cls,
        *,
        tool: EvaluatorToolRef,
        parameters: JsonObject,
        dispatch_name: str,
    ) -> JsonObject:
        if dispatch_name != "shell.exec_readonly":
            # Backward compatibility for older browser probe contracts. The Web evaluator now dispatches
            # namespaced `web.web_list_tabs` directly through the controller/device allowlist.
            if tool.name == "client_browser_tabs" and dispatch_name == "browser_tabs":
                return {"action": "list"}
            return parameters
        if tool.name == "client_bash_readonly":
            return {"cmd": validate_readonly_shell_command(str(parameters.get("cmd") or ""))}
        return parameters

    @staticmethod
    def _matching_command_result(
        *,
        call_id: str,
        results: list[CommandExecutionResultRecord],
    ) -> CommandExecutionResultRecord | None:
        for result in results:
            if result.call_id == call_id or str(result.call_id).endswith(call_id):
                return result
        return results[0] if results else None

    @classmethod
    def _payload_from_command_result(
        cls,
        result: CommandExecutionResultRecord,
        *,
        model_tool_name: str,
    ) -> JsonObject:
        payload = cls._strip_inline_images(result.result)
        return {
            "status": "success" if result.status == ResultStatus.SUCCESS else "failure",
            "tool": model_tool_name,
            "result": payload,
            "error": cls._sanitize_error_text(result.error),
        }

    @classmethod
    def _payload_from_tool_block(
        cls,
        block: object,
        *,
        model_tool_name: str,
        dispatch_name: str,
    ) -> JsonObject:
        content_value = getattr(block, "content", None)
        content = content_value if isinstance(content_value, list) else [content_value]
        text_parts = []
        image_count = 0
        for item in content:
            text_value = cls._text_from_tool_block_item(item)
            if text_value is not None:
                text_parts.append(cls._sanitize_text(text_value))
            elif cls._is_image_tool_block_item(item):
                image_count += 1
            elif item is not None:
                text_parts.append(cls._sanitize_text(str(item)))
        return {
            "status": "failure" if bool(getattr(block, "is_error", False)) else "success",
            "tool": model_tool_name,
            "dispatch_name": dispatch_name,
            "text": "\n".join(part for part in text_parts if part),
            "image_omitted": image_count > 0,
            "image_count": image_count,
        }

    @staticmethod
    def _text_from_tool_block_item(item: object) -> str | None:
        if isinstance(item, dict):
            value = item.get("text") or item.get("content")
            return value if isinstance(value, str) else None
        value = getattr(item, "text", None)
        return value if isinstance(value, str) else None

    @staticmethod
    def _is_image_tool_block_item(item: object) -> bool:
        if isinstance(item, dict):
            item_type = str(item.get("type") or "")
            return "image" in item_type or "image_url" in item or "data" in item
        class_name = type(item).__name__.lower()
        return "image" in class_name or hasattr(item, "image_url") or hasattr(item, "data")

    @classmethod
    def _strip_inline_images(cls, value: object) -> object:
        return cls._sanitize_result_value(value)

    @classmethod
    def _sanitize_result_value(cls, value: object, *, key_hint: str = "") -> object:
        if isinstance(value, str):
            if _DATA_IMAGE_URL_RE.match(value):
                return f"[image data omitted; chars={len(value)}]"
            if key_hint in _PATH_RESULT_KEYS or key_hint.endswith("_path"):
                return "[path omitted]" if value else value
            if key_hint in _RAW_ERROR_RESULT_KEYS and value.strip():
                return f"[redacted {key_hint}]"
            return cls._sanitize_text(value)
        if isinstance(value, dict):
            sanitized: JsonObject = {}
            for key, item in value.items():
                key_text = str(key)
                sanitized[key_text] = cls._sanitize_result_value(item, key_hint=key_text)
                if key_text in _RAW_ERROR_RESULT_KEYS and isinstance(item, str) and item.strip():
                    sanitized[f"{key_text}_present"] = True
            return sanitized
        if isinstance(value, list):
            return [cls._sanitize_result_value(item, key_hint=key_hint) for item in value]
        return value

    @classmethod
    def _sanitize_error_text(cls, value: object) -> str | None:
        if value is None:
            return None
        return cls._sanitize_text(str(value))

    @classmethod
    def _sanitize_text(cls, value: str) -> str:
        text = value
        text = _INLINE_DATA_IMAGE_URL_RE.sub(lambda match: f"[image data omitted; chars={len(match.group(0))}]", text)
        text = _WINDOWS_PATH_RE.sub("[path omitted]", text)
        text = _ABSOLUTE_PATH_RE.sub(lambda match: f"{match.group(1)}[path omitted]", text)
        return text


__all__ = [
    "EvaluatorClientToolDispatcher",
    "ExternalEvaluatorToolDispatcher",
    "LocalEvaluatorClientToolDispatcher",
    "RemoteEvaluatorClientToolDispatcher",
    "UnavailableEvaluatorClientToolDispatcher",
]
