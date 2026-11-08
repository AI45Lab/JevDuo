"""Provider compatibility facade for the public NexAU migration.

Only provider-boundary behavior lives here.  The facade returns and consumes
the installed NexAU classes, preserving type identity while making migration
flags explicit and independently testable.
"""

from __future__ import annotations

import copy
import inspect
import threading
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import Enum
from functools import wraps
from typing import Literal, Protocol, cast

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent_state import AgentState
from nexau.archs.main_sub.execution.hooks import MiddlewareManager
from nexau.archs.main_sub.execution.llm_caller import LLMCaller
from nexau.archs.main_sub.execution.model_response import ModelResponse
from nexau.archs.main_sub.execution.stop_reason import AgentStopReason
from nexau.archs.main_sub.token_trace_session import TokenTraceSession
from nexau.archs.tool.tool import StructuredToolDefinitionLike, structured_tool_definition_to_anthropic
from nexau.core.messages import DiscriminatedBlock, Message, ReasoningBlock, TextBlock
from nexau.core.serializers.anthropic_messages import serialize_ump_to_anthropic_messages_payload
from nexau.core.serializers.openai_chat import serialize_ump_to_openai_chat_payload
from nexau.core.serializers.openai_responses import (
    prepare_openai_responses_api_input as _public_prepare_openai_responses_api_input,
)

from northcua.agentkernel.tools.surface import active_provider_tool_payload
from northcua.core.json_types import JsonObject
from northcua.policy_invocation import policy_step_extra_headers
from northcua_runtime_shared.tool_contracts import apply_openai_union_tool_policy

from .anthropic import LegacyAnthropicSignatureError, wrap_anthropic_client
from .openai_responses import normalize_legacy_openai_responses_items, wrap_openai_responses_client
from .responses import normalize_legacy_model_response

ToolImagePolicy = Literal["inject_user_message", "embed_in_tool_message"]


class _AnthropicSerializerWithFlag(Protocol):
    def __call__(
        self,
        messages: list[Message],
        *,
        allow_unsigned_thinking: bool,
    ) -> tuple[list[dict[str, object]], list[dict[str, object]]]: ...


class UnsignedReasoningReplayPolicy(str, Enum):
    """Anthropic replay behavior for reasoning without a provider signature."""

    LEGACY_TEXT = "legacy_text"
    DROP = "drop"
    PROVIDER_THINKING = "provider_thinking"


@dataclass(frozen=True, slots=True)
class NexAUProviderCompatibilityPolicy:
    """Flags whose public defaults could otherwise change provider payloads."""

    unsigned_reasoning: UnsignedReasoningReplayPolicy = UnsignedReasoningReplayPolicy.LEGACY_TEXT
    # Private 58d3fa4 always emitted Anthropic eager_input_streaming.
    anthropic_tool_streaming: bool = True
    # Kept false because legacy replay is projected to TextBlock explicitly.
    public_allow_unsigned_thinking: bool = False
    # Public c98f7fb repairs invalid signed thinking inside one provider call;
    # private 58d3fa4 retries the unchanged payload at the outer caller layer.
    preserve_invalid_signature_outer_retry: bool = True
    openai_chat_tool_image_policy: ToolImagePolicy = "inject_user_message"


@dataclass(frozen=True, slots=True)
class NexAUProviderCapabilities:
    """Provider features exposed by the installed NexAU distribution."""

    anthropic_unsigned_thinking_flag: bool
    llm_config_tool_streaming: bool
    llm_config_allow_unsigned_thinking: bool
    llm_call_framework_context: bool
    anthropic_tool_streaming_parameter: bool

    @classmethod
    def detect(cls) -> "NexAUProviderCapabilities":
        """Probe signatures once at the centralized dependency boundary."""

        anthropic_params = inspect.signature(serialize_ump_to_anthropic_messages_payload).parameters
        llm_config_params = inspect.signature(LLMConfig.__init__).parameters
        llm_call_params = inspect.signature(LLMCaller.call_llm).parameters
        anthropic_tool_params = inspect.signature(structured_tool_definition_to_anthropic).parameters
        return cls(
            anthropic_unsigned_thinking_flag="allow_unsigned_thinking" in anthropic_params,
            llm_config_tool_streaming="tool_streaming" in llm_config_params,
            llm_config_allow_unsigned_thinking="allow_unsigned_thinking" in llm_config_params,
            llm_call_framework_context="framework_context" in llm_call_params,
            anthropic_tool_streaming_parameter="tool_streaming" in anthropic_tool_params,
        )


@dataclass(frozen=True, slots=True)
class OpenAIResponsesInput:
    """Prepared OpenAI Responses input and optional system instructions."""

    items: list[JsonObject]
    instructions: str | None


@dataclass(frozen=True, slots=True)
class AnthropicMessagesInput:
    """Prepared Anthropic system and conversation payloads."""

    system: list[JsonObject]
    messages: list[JsonObject]


@dataclass(slots=True)
class LegacyLLMCallRequest:
    """The private-pin LLMCaller call contract, without public-only kwargs."""

    messages: list[Message]
    api_type: str | None = None
    max_tokens: int | None = None
    force_stop_reason: AgentStopReason | None = None
    agent_state: AgentState | None = None
    tool_call_mode: str = "xml"
    tools: Sequence[StructuredToolDefinitionLike] | None = None
    openai_client: object | None = None
    shutdown_event: threading.Event | None = None
    token_trace_session: TokenTraceSession | None = None


class NexAUProviderCapabilityError(RuntimeError):
    """Raised when a requested provider policy is unsupported by NexAU."""


class NexAUProviderAdapter:
    """Make private-pin provider behavior explicit over the installed NexAU."""

    def __init__(
        self,
        policy: NexAUProviderCompatibilityPolicy | None = None,
        capabilities: NexAUProviderCapabilities | None = None,
    ) -> None:
        self._policy = policy or NexAUProviderCompatibilityPolicy()
        self._capabilities = capabilities or NexAUProviderCapabilities.detect()
        if (
            self._policy.unsigned_reasoning == UnsignedReasoningReplayPolicy.PROVIDER_THINKING
            and not self._policy.public_allow_unsigned_thinking
        ):
            raise ValueError("provider_thinking replay requires public_allow_unsigned_thinking=true")
        if self._policy.public_allow_unsigned_thinking and not self._capabilities.anthropic_unsigned_thinking_flag:
            raise NexAUProviderCapabilityError("installed NexAU cannot explicitly serialize unsigned thinking")

    @property
    def policy(self) -> NexAUProviderCompatibilityPolicy:
        """Return the immutable provider policy."""

        return self._policy

    @property
    def capabilities(self) -> NexAUProviderCapabilities:
        """Return the detected provider surface."""

        return self._capabilities

    def prepare_llm_config(self, llm_config: LLMConfig) -> LLMConfig:
        """Clone a config and set public migration flags explicitly."""

        prepared = llm_config.copy()
        if self._capabilities.llm_config_tool_streaming:
            setattr(prepared, "tool_streaming", self._policy.anthropic_tool_streaming)
        if self._capabilities.llm_config_allow_unsigned_thinking:
            setattr(prepared, "allow_unsigned_thinking", self._policy.public_allow_unsigned_thinking)
        return prepared

    def create_llm_caller(
        self,
        *,
        openai_client: object,
        llm_config: LLMConfig,
        retry_attempts: int = 5,
        retry_backoff_max_seconds: int = 30,
        on_retry: Callable[[int, int, float, str], None] | None = None,
        middleware_manager: MiddlewareManager | None = None,
        global_storage: object | None = None,
        session_id: str | None = None,
        async_openai_client: object | None = None,
    ) -> LLMCaller:
        """Construct the real NexAU caller with an explicit compatibility config."""

        caller = LLMCaller(
            openai_client=openai_client,
            async_openai_client=async_openai_client,
            llm_config=self.prepare_llm_config(llm_config),
            retry_attempts=retry_attempts,
            retry_backoff_max_seconds=retry_backoff_max_seconds,
            on_retry=on_retry,
            middleware_manager=middleware_manager,
            global_storage=global_storage,
            session_id=session_id,
        )
        self.install_on_caller(caller)
        return caller

    def install_on_caller(self, caller: LLMCaller) -> None:
        """Install provider projection at NexAU's immutable retry boundary.

        NorthCUA's stepwise execution calls this adapter directly, while the
        complete Agent loop invokes ``LLMCaller`` from inside NexAU.  Wrapping
        the normalized retry entrypoints keeps both paths equivalent and also
        handles a middleware-selected fallback provider.  The wrapper copies
        ``ModelCallParams`` before replacing messages, so history, hooks, and
        trace inputs retain the private-pin UMP representation.
        """

        marker = "__northcua_provider_compat_installed__"
        if getattr(caller, marker, False):
            return
        llm_config = getattr(caller, "llm_config", None)
        api_type = str(getattr(llm_config, "api_type", ""))
        if hasattr(caller, "openai_client"):
            caller.openai_client = self._prepare_client(api_type, caller.openai_client)
        if hasattr(caller, "async_openai_client"):
            caller.async_openai_client = self._prepare_client(api_type, caller.async_openai_client)
        self._wrap_retry_projection(caller, "_call_with_retry")
        self._wrap_async_retry_projection(caller, "_call_with_retry_async")
        setattr(caller, marker, True)

    def _wrap_retry_projection(self, caller: LLMCaller, name: str) -> None:
        original = getattr(caller, name, None)
        if not callable(original):
            raise NexAUProviderCapabilityError(f"installed NexAU has no normalized {name} boundary")
        typed_original = cast(Callable[..., ModelResponse | None], original)

        @wraps(typed_original)
        def projected(params: object, *args: object, **kwargs: object) -> ModelResponse | None:
            try:
                projected_params = self._project_model_call_params(caller, params)
                response = typed_original(projected_params, *args, **kwargs)
                return normalize_legacy_model_response(
                    self._model_call_api_type(caller, projected_params),
                    response,
                    stream=self._model_call_streaming(caller, projected_params),
                )
            except LegacyAnthropicSignatureError as exc:
                raise exc.original from None

        setattr(caller, name, projected)

    def _wrap_async_retry_projection(self, caller: LLMCaller, name: str) -> None:
        original = getattr(caller, name, None)
        if not callable(original):
            raise NexAUProviderCapabilityError(f"installed NexAU has no normalized {name} boundary")
        typed_original = cast(Callable[..., Awaitable[ModelResponse | None]], original)

        @wraps(typed_original)
        async def projected(params: object, *args: object, **kwargs: object) -> ModelResponse | None:
            try:
                self._refresh_async_client(caller, params)
                projected_params = self._project_model_call_params(caller, params)
                response = await typed_original(projected_params, *args, **kwargs)
                return normalize_legacy_model_response(
                    self._model_call_api_type(caller, projected_params),
                    response,
                    stream=self._model_call_streaming(caller, projected_params),
                )
            except LegacyAnthropicSignatureError as exc:
                raise exc.original from None

        setattr(caller, name, projected)

    def _refresh_async_client(self, caller: LLMCaller, params: object) -> None:
        """Wrap the current lazy async client before every provider attempt.

        Public NexAU closes and clears the native async client after each
        ``Agent.run_async`` and creates a fresh client at the next run. The
        caller-level retry wrapper survives that lifecycle, so this boundary
        must reapply per-client compatibility to the newly assigned instance.
        ``params.llm_config`` selects the active fallback candidate when one is
        present.
        """

        if not hasattr(caller, "async_openai_client"):
            return
        current = caller.async_openai_client
        prepared = self._prepare_client(self._model_call_api_type(caller, params), current)
        if prepared is not current:
            caller.async_openai_client = prepared

    def _project_model_call_params(self, caller: LLMCaller, params: object) -> object:
        messages = getattr(params, "messages", None)
        if not isinstance(messages, list) or not all(isinstance(message, Message) for message in messages):
            return params
        llm_config = getattr(params, "llm_config", None) or getattr(caller, "llm_config", None)
        api_type = str(getattr(llm_config, "api_type", ""))
        prepared = self.prepare_messages_for_api_type(api_type, messages)
        client = getattr(params, "openai_client", None)
        prepared_client = self._prepare_client(api_type, client)
        api_params = getattr(params, "api_params", None)
        projected_api_params: object = api_params
        active_tools = active_provider_tool_payload(caller)
        if active_tools is not None:
            active_params: dict[str, object] = (
                dict(cast(Mapping[str, object], api_params)) if isinstance(api_params, Mapping) else {}
            )
            active_params["tools"] = active_tools
            projected_api_params = active_params
        if api_type in {"openai_chat_completion", "openai_responses"} and isinstance(projected_api_params, Mapping):
            raw_tools = projected_api_params.get("tools")
            if isinstance(raw_tools, list) and all(isinstance(tool, Mapping) for tool in raw_tools):
                projected_tools = apply_openai_union_tool_policy(cast(list[Mapping[str, object]], raw_tools))
                if projected_tools != raw_tools:
                    normalized_params = dict(cast(Mapping[str, object], projected_api_params))
                    normalized_params["tools"] = projected_tools
                    projected_api_params = normalized_params
        step_headers = policy_step_extra_headers()
        if api_type == "openai_chat_completion" and step_headers:
            projected_dict: dict[str, object] = (
                dict(cast(Mapping[str, object], projected_api_params))
                if isinstance(projected_api_params, Mapping)
                else {}
            )
            existing_headers = projected_dict.get("extra_headers")
            if existing_headers is not None and not isinstance(existing_headers, Mapping):
                raise NexAUProviderCapabilityError("OpenAI extra_headers must be a mapping")
            merged_headers = (
                dict(cast(Mapping[str, object], existing_headers)) if isinstance(existing_headers, Mapping) else {}
            )
            for name, value in step_headers.items():
                existing = merged_headers.get(name)
                if existing is not None and existing != value:
                    raise NexAUProviderCapabilityError("Controller step header conflicts with provider parameters")
                merged_headers[name] = value
            projected_dict["extra_headers"] = merged_headers
            projected_api_params = projected_dict
        if prepared is messages and prepared_client is client and projected_api_params is api_params:
            return params
        projected = copy.copy(params)
        setattr(projected, "messages", prepared)
        if prepared_client is not client:
            setattr(projected, "openai_client", prepared_client)
        if projected_api_params is not api_params:
            setattr(projected, "api_params", projected_api_params)
        return projected

    def _prepare_client(self, api_type: str, client: object | None) -> object | None:
        if self._policy.preserve_invalid_signature_outer_retry and api_type == "anthropic_chat_completion":
            return wrap_anthropic_client(client)
        if api_type == "openai_responses":
            return wrap_openai_responses_client(client)
        return client

    @staticmethod
    def _model_call_api_type(caller: LLMCaller, params: object) -> str:
        llm_config = getattr(params, "llm_config", None) or getattr(caller, "llm_config", None)
        return str(getattr(llm_config, "api_type", ""))

    @staticmethod
    def _model_call_streaming(caller: LLMCaller, params: object) -> bool:
        llm_config = getattr(params, "llm_config", None) or getattr(caller, "llm_config", None)
        api_params = getattr(params, "api_params", None)
        configured = bool(getattr(llm_config, "stream", False))
        requested = bool(api_params.get("stream", False)) if isinstance(api_params, Mapping) else False
        return configured or requested

    def call_llm(self, caller: LLMCaller, request: LegacyLLMCallRequest) -> ModelResponse | None:
        """Call NexAU through the private-pin invocation surface."""

        messages = self.prepare_messages_for_api_type(self._api_type(caller, request), request.messages)
        return caller.call_llm(
            messages=messages,
            max_tokens=request.max_tokens,
            force_stop_reason=request.force_stop_reason,
            agent_state=request.agent_state,
            tool_call_mode=request.tool_call_mode,
            tools=request.tools,
            openai_client=request.openai_client,
            shutdown_event=request.shutdown_event,
            token_trace_session=request.token_trace_session,
        )

    async def call_llm_async(self, caller: LLMCaller, request: LegacyLLMCallRequest) -> ModelResponse | None:
        """Async equivalent of :meth:`call_llm`."""

        messages = self.prepare_messages_for_api_type(self._api_type(caller, request), request.messages)
        return await caller.call_llm_async(
            messages=messages,
            max_tokens=request.max_tokens,
            force_stop_reason=request.force_stop_reason,
            agent_state=request.agent_state,
            tool_call_mode=request.tool_call_mode,
            tools=request.tools,
            openai_client=request.openai_client,
            shutdown_event=request.shutdown_event,
            token_trace_session=request.token_trace_session,
        )

    def prepare_messages_for_api_type(self, api_type: str, messages: list[Message]) -> list[Message]:
        """Apply only provider-specific legacy projections."""

        if api_type == "anthropic_chat_completion":
            return self._project_anthropic_reasoning(messages)
        return messages

    @staticmethod
    def _api_type(caller: object, request: LegacyLLMCallRequest) -> str:
        if request.api_type is not None:
            return request.api_type
        llm_config = getattr(caller, "llm_config", None)
        return str(getattr(llm_config, "api_type", ""))

    def serialize_openai_chat(
        self,
        messages: list[Message],
        *,
        tool_image_policy: ToolImagePolicy | None = None,
    ) -> list[JsonObject]:
        """Serialize OpenAI Chat with an explicit image policy."""

        payload = serialize_ump_to_openai_chat_payload(
            messages,
            tool_image_policy=tool_image_policy or self._policy.openai_chat_tool_image_policy,
        )
        return cast(list[JsonObject], payload)

    @staticmethod
    def prepare_openai_responses_input(chat_messages: list[JsonObject]) -> OpenAIResponsesInput:
        """Prepare chat-shaped input and remove public-only empty reasoning.

        Private ``58d3fa4`` dropped empty plain/signed reasoning and retained
        encrypted reasoning with an empty summary list. Public ``c98f7fb``
        synthesizes an empty ``summary_text`` item in both cases. Normalize at
        the final Responses input boundary so diagnostics and the real caller
        share the same contract.
        """

        items, instructions = _public_prepare_openai_responses_api_input(cast(list[dict[str, object]], chat_messages))
        return OpenAIResponsesInput(
            items=normalize_legacy_openai_responses_items(cast(list[JsonObject], items)),
            instructions=instructions,
        )

    def serialize_anthropic(self, messages: list[Message]) -> AnthropicMessagesInput:
        """Serialize Anthropic with explicit legacy unsigned-reasoning replay."""

        prepared = self._project_anthropic_reasoning(messages)
        if self._capabilities.anthropic_unsigned_thinking_flag:
            serializer = cast(_AnthropicSerializerWithFlag, serialize_ump_to_anthropic_messages_payload)
            system, conversation = serializer(
                prepared,
                allow_unsigned_thinking=self._policy.public_allow_unsigned_thinking,
            )
        else:
            system, conversation = serialize_ump_to_anthropic_messages_payload(prepared)
        return AnthropicMessagesInput(
            system=cast(list[JsonObject], system),
            messages=cast(list[JsonObject], conversation),
        )

    def serialize_anthropic_tool(self, definition: StructuredToolDefinitionLike) -> JsonObject:
        """Serialize one tool with the private eager-streaming contract."""

        if self._capabilities.anthropic_tool_streaming_parameter:
            payload = structured_tool_definition_to_anthropic(definition, tool_streaming=True)
        else:
            payload = structured_tool_definition_to_anthropic(definition)
        return cast(JsonObject, payload)

    def _project_anthropic_reasoning(self, messages: list[Message]) -> list[Message]:
        policy = self._policy.unsigned_reasoning
        if policy == UnsignedReasoningReplayPolicy.PROVIDER_THINKING:
            if not self._capabilities.anthropic_unsigned_thinking_flag:
                raise NexAUProviderCapabilityError("installed NexAU cannot emit unsigned provider thinking")
            return messages

        projected: list[Message] = []
        for message in messages:
            changed = False
            content: list[DiscriminatedBlock] = []
            for block in message.content:
                if not isinstance(block, ReasoningBlock) or block.signature or block.redacted_data:
                    content.append(block)
                    continue
                changed = True
                if policy == UnsignedReasoningReplayPolicy.LEGACY_TEXT:
                    content.append(TextBlock(text=block.text))
            projected.append(message.model_copy(update={"content": content}) if changed else message)
        return projected


__all__ = [
    "AnthropicMessagesInput",
    "LegacyLLMCallRequest",
    "NexAUProviderAdapter",
    "NexAUProviderCapabilities",
    "NexAUProviderCapabilityError",
    "NexAUProviderCompatibilityPolicy",
    "OpenAIResponsesInput",
    "UnsignedReasoningReplayPolicy",
]
