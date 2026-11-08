"""Resolve the actual provider-facing tool surface from a prepared NexAU agent."""

from __future__ import annotations

from collections.abc import Iterable
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, cast

from northcua.agentkernel.json_types import ProviderToolChoice, ProviderToolSpecs
from northcua_runtime_shared.tool_contracts import ResolvedToolSurface

_ACTIVE_PROVIDER_TOOL_PAYLOAD = "__northcua_active_provider_tool_payload__"


@dataclass(slots=True)
class ToolSurface:
    """Prompt/trace-facing tool surface resolved from the live NexAU agent.

    Attributes:
        tools: Provider-normalized tool definitions.
        tool_choice: Provider-normalized tool-choice setting.
    """

    tools: ProviderToolSpecs = field(default_factory=list)
    tool_choice: ProviderToolChoice = None


class ToolSurfaceService:
    """Build prompt/trace tool metadata from the actual agent runtime."""

    def activate_for_agent(
        self,
        agent: Any,
        *,
        tool_choice_policy: str = "auto",
        allowed_tool_names: Iterable[str] = (),
    ) -> ToolSurface:
        """Resolve and install the exact provider-facing tool set for one model step."""

        allowed_names = tuple(allowed_tool_names)
        surface, structured_tools = self._resolve_for_agent(
            agent,
            tool_choice_policy=tool_choice_policy,
            allowed_tool_names=allowed_names,
        )
        executor = getattr(agent, "executor", None)
        update_tools = getattr(executor, "update_structured_tools", None)
        requires_install = bool(allowed_names) or tool_choice_policy != "auto"
        if not callable(update_tools) and structured_tools and requires_install:
            raise TypeError("prepared agent executor cannot install a resolved tool surface")
        if callable(update_tools):
            update_tools(structured_tools)
        # NexAU derives ``Agent.tool_call_payload`` from the full execution
        # registry and its Executor may re-add every eager tool when taking a
        # payload snapshot.  Keep the task-scoped provider surface separate
        # from that execution registry so NorthCUA's one-step transport can
        # send exactly the allowlisted definitions without deleting the Tool
        # objects needed to execute an allowed call.
        install_active_provider_tool_payload(agent, structured_tools)
        llm_caller = getattr(executor, "llm_caller", None)
        if llm_caller is not None:
            # Full NexAU Agent loops rebuild executor payloads from the complete
            # execution registry immediately before provider retry.  Bind the
            # normalized task surface to the immutable retry boundary too, so
            # both stepwise and compatibility paths send the same allowlist.
            install_active_provider_tool_payload(llm_caller, surface.tools)
        return surface

    def build_for_agent(
        self,
        agent: Any,
        *,
        tool_choice_policy: str = "auto",
        allowed_tool_names: Iterable[str] = (),
    ) -> ToolSurface:
        """Resolve the current eager tool surface from the prepared agent.

        Args:
            agent: Prepared NexAU agent with eager tool payloads.
            tool_choice_policy: Neutral per-task policy, either ``auto`` or
                ``required``.

        Returns:
            Provider-facing tool surface for prompt and trace capture.
        """

        surface, _structured_tools = self._resolve_for_agent(
            agent,
            tool_choice_policy=tool_choice_policy,
            allowed_tool_names=allowed_tool_names,
        )
        return surface

    def _resolve_for_agent(
        self,
        agent: Any,
        *,
        tool_choice_policy: str,
        allowed_tool_names: Iterable[str],
    ) -> tuple[ToolSurface, list[Any]]:
        from nexau.archs.tool.tool import StructuredToolDefinitionLike

        from northcua.agentinfra.providers.provider_payloads import (
            adapt_structured_tools_for_api_type,
            default_tool_choice_for_api_type,
        )

        structured_tools = list(agent.tool_call_payload)
        allowed = frozenset(allowed_tool_names)
        if allowed:
            structured_tools = [tool for tool in structured_tools if _provider_tool_name(tool) in allowed]
            missing = allowed - {_provider_tool_name(tool) for tool in structured_tools}
            if missing:
                raise ValueError(
                    f"allowed tool name(s) are absent from the runtime surface: {', '.join(sorted(missing))}"
                )

        api_type = agent.config.llm_config.api_type if agent.config.llm_config is not None else ""
        normalized_tools = adapt_structured_tools_for_api_type(
            tool_definitions=cast(list[StructuredToolDefinitionLike], structured_tools),
            api_type=api_type,
        )
        tool_choice = _resolve_tool_choice(
            api_type,
            tools_present=bool(normalized_tools),
            policy=tool_choice_policy,
            default_resolver=default_tool_choice_for_api_type,
        )
        return ToolSurface(tools=normalized_tools, tool_choice=tool_choice), structured_tools

    def build_from_resolved_surface(
        self,
        surface: ResolvedToolSurface,
        *,
        api_type: str = "",
        tool_choice_policy: str = "auto",
    ) -> ToolSurface:
        """Build provider metadata from the canonical resolved tool surface."""

        structured_tools = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                    **({"strict": False} if not tool.strict else {}),
                },
            }
            for tool in surface.provider_tools
        ]
        if not structured_tools:
            if tool_choice_policy == "auto":
                return ToolSurface()
            if tool_choice_policy == "required":
                raise ValueError("required tool choice policy needs at least one model-visible tool")
            raise ValueError(f"unsupported tool choice policy: {tool_choice_policy}")
        from nexau.archs.tool.tool import StructuredToolDefinitionLike

        from northcua.agentinfra.providers.provider_payloads import (
            adapt_structured_tools_for_api_type,
            default_tool_choice_for_api_type,
        )

        normalized_tools = adapt_structured_tools_for_api_type(
            tool_definitions=cast(list[StructuredToolDefinitionLike], structured_tools),
            api_type=api_type,
        )
        tool_choice = _resolve_tool_choice(
            api_type,
            tools_present=bool(normalized_tools),
            policy=tool_choice_policy,
            default_resolver=default_tool_choice_for_api_type,
        )
        return ToolSurface(tools=normalized_tools, tool_choice=tool_choice)


def _resolve_tool_choice(
    api_type: str,
    *,
    tools_present: bool,
    policy: str,
    default_resolver: Any,
) -> ProviderToolChoice:
    """Translate one neutral task policy into the provider-native wire shape."""

    if policy == "auto":
        return cast(ProviderToolChoice, default_resolver(api_type, tools_present=tools_present))
    if policy != "required":
        raise ValueError(f"unsupported tool choice policy: {policy}")
    if not tools_present:
        raise ValueError("required tool choice policy needs at least one model-visible tool")
    if api_type in {"openai_chat_completion", "openai_responses"}:
        return "required"
    if api_type == "anthropic_chat_completion":
        return {"type": "any"}
    raise ValueError(f"required tool choice policy is unsupported for provider API: {api_type or '<empty>'}")


ToolSurfaceBuilder = ToolSurfaceService


def active_provider_tool_payload(agent: Any) -> list[Any] | None:
    """Return the installed task-scoped provider payload, if one exists."""

    payload = getattr(agent, _ACTIVE_PROVIDER_TOOL_PAYLOAD, None)
    if payload is None:
        return None
    if not isinstance(payload, list):
        raise TypeError("active provider tool payload must be a list")
    return deepcopy(payload)


def install_active_provider_tool_payload(owner: Any, payload: Iterable[Any]) -> None:
    """Install one task-scoped provider payload on an agent or LLM caller."""

    setattr(owner, _ACTIVE_PROVIDER_TOOL_PAYLOAD, deepcopy(list(payload)))


def normalize_allowed_tool_names(value: object) -> tuple[str, ...]:
    """Validate task metadata before filtering a provider tool surface."""

    if value is None:
        return ()
    if not isinstance(value, (list, tuple)):
        raise ValueError("allowed_tool_names task metadata must be a list")
    names = tuple(str(item) for item in value)
    if len(names) != len(set(names)) or any(not name or name.strip() != name for name in names):
        raise ValueError("allowed_tool_names task metadata must contain unique non-empty names")
    return names


def _provider_tool_name(tool: object) -> str:
    if not isinstance(tool, dict):
        return ""
    function = tool.get("function")
    if isinstance(function, dict):
        return str(function.get("name") or tool.get("name") or "")
    return str(tool.get("name") or "")
