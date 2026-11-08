"""Evaluator live-probe dispatch bound to an existing Web agent session."""

from __future__ import annotations

import re

from pydantic import BaseModel, ValidationError

from northcua.agentkit.evaluator.routing import EvaluatorClientToolDispatcher
from northcua.agentkit.evaluator.spec import EvaluatorToolRef
from northcua.core.json_types import JsonObject
from northcua.runtime.mcp.invocation_context import MCPInvocationContext, use_mcp_invocation_context

from ...backend import WebActionBackend
from ...domain.actions import WebAgentActionType, action_from_tool_name
from ...mcp.action_server import _web_action_handler_map
from ...mcp.action_service import WebAgentMCPActionService
from ...mcp.descriptors import web_agent_action_contracts
from ...session import RegisteredWebAgentSession, WebAgentSessionRegistry

_INLINE_DATA_IMAGE_URL_RE = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=_-]+", re.IGNORECASE)
_ABSOLUTE_PATH_RE = re.compile(r"(^|[\s'\"({\[=])/(?:[^\s'\"<>),}\]]+)")
_WINDOWS_PATH_RE = re.compile(r"\b[A-Za-z]:\\[^\s'\"<>]+")
_RAW_ERROR_KEYS = {"capture_error", "image_omitted_reason"}


class WebBrowserLiveProbeDispatcher(EvaluatorClientToolDispatcher):
    """Dispatch evaluator `client_browser_*` probes through the active Web backend."""

    def __init__(self, *, registered_session: RegisteredWebAgentSession) -> None:
        self.registered_session = registered_session
        self.registry = WebAgentSessionRegistry()
        self.registry.register(registered_session)
        self.service = WebAgentMCPActionService(registry=self.registry)
        self._handlers = _web_action_handler_map(self.service)
        self._contracts = {contract.provider_name: contract for contract in web_agent_action_contracts()}

    @property
    def client(self) -> WebActionBackend:
        """Return the browser backend owned by the evaluated Web session."""

        return self.registered_session.client

    async def dispatch(
        self,
        *,
        tool: EvaluatorToolRef,
        parameters: JsonObject,
        remote: bool,
    ) -> JsonObject:
        """Execute an evaluator browser tool in the evaluated Web session."""

        if remote:
            return {
                "status": "failure",
                "tool": tool.name,
                "error": "WebBrowserLiveProbeDispatcher is local-only; use RemoteEvaluatorClientToolDispatcher.",
            }
        dispatch_name = _web_action_dispatch_key(tool.dispatch_name)
        handler = self._handlers.get(dispatch_name)
        contract = self._contracts.get(dispatch_name)
        if handler is None or contract is None:
            return {
                "status": "failure",
                "tool": tool.name,
                "dispatch_name": dispatch_name,
                "error": f"Unsupported local Web live probe wrapper: {tool.name}",
            }
        try:
            parsed_parameters = _parse_parameters(contract.params_model, parameters)
        except ValidationError as exc:
            action = action_from_tool_name(dispatch_name) or WebAgentActionType.WAIT
            return _validation_failure(tool.name, action=action, exc=exc)
        return await self._dispatch_web_action(tool_name=tool.name, call=lambda: handler(parsed_parameters))

    async def _dispatch_web_action(self, *, tool_name: str, call) -> JsonObject:
        with use_mcp_invocation_context(MCPInvocationContext(session_id=self.registered_session.session_id)):
            payload = await call()
        status = "failure" if isinstance(payload, dict) and payload.get("status") == "error" else "success"
        safe_payload = _safe_web_action_payload(payload)
        error_text = str(safe_payload.get("error") or "") if isinstance(safe_payload, dict) else ""
        return {
            "status": status,
            "tool": tool_name,
            "result": safe_payload,
            "error": error_text if status == "failure" else None,
        }


def _safe_web_action_payload(value: object, *, key_hint: str = "") -> object:
    if isinstance(value, str):
        if value.lower().startswith("data:image/"):
            return "[image data omitted]"
        if key_hint.endswith("_path") or key_hint in {"path", "source_screenshot_path", "overlay_screenshot_path"}:
            return "[path omitted]" if value else value
        return _sanitize_text(value)
    if isinstance(value, dict):
        output: JsonObject = {}
        for key, item in value.items():
            key_text = str(key)
            if key_text == "raw":
                if isinstance(item, dict) and str(item.get("capture_error") or "").strip():
                    output["capture_error"] = "[redacted capture error]"
                    output["capture_error_present"] = True
                continue
            if key_text in {"screenshot_url", "image_url", "data_url"} and isinstance(item, str):
                output[key_text] = f"[image data omitted; chars={len(item)}]"
                continue
            if key_text in _RAW_ERROR_KEYS and isinstance(item, str) and item.strip():
                output[key_text] = f"[redacted {key_text}]"
                output[f"{key_text}_present"] = True
                continue
            output[key_text] = _safe_web_action_payload(item, key_hint=key_text)
        return output
    if isinstance(value, list):
        return [_safe_web_action_payload(item, key_hint=key_hint) for item in value]
    return value


def _web_action_dispatch_key(dispatch_name: str) -> str:
    """Normalize controller-qualified Web tool ids to local MCP handler names."""

    if dispatch_name.startswith("web."):
        return dispatch_name.split(".", 1)[1]
    return dispatch_name


def _parse_parameters(params_model: type[BaseModel] | None, parameters: JsonObject) -> object:
    if params_model is None:
        return {}
    return params_model.model_validate(parameters)


def _sanitize_text(value: str) -> str:
    text = value
    text = _INLINE_DATA_IMAGE_URL_RE.sub("[image data omitted]", text)
    text = _WINDOWS_PATH_RE.sub("[path omitted]", text)
    text = _ABSOLUTE_PATH_RE.sub(lambda match: f"{match.group(1)}[path omitted]", text)
    return text


def _validation_failure(tool_name: str, *, action: WebAgentActionType, exc: ValidationError) -> JsonObject:
    safe_error = _validation_error_message(exc)
    return {
        "status": "failure",
        "tool": tool_name,
        "result": {
            "status": "error",
            "action": action.value,
            "code": "validation_error",
            "error": safe_error,
            "error_type": type(exc).__name__,
            "retryable": False,
        },
        "error": safe_error,
    }


def _validation_error_message(exc: ValidationError) -> str:
    messages: list[str] = []
    for error in exc.errors(include_input=False):
        message = str(error.get("msg") or error.get("type") or "").strip()
        if message:
            messages.append(message)
    return "; ".join(messages) if messages else "Invalid web screenshot parameters."
