"""Runtime invocation and constructed-Agent compatibility adapter for NexAU."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import wraps
from threading import RLock
from typing import Any, TypeVar, cast

from nexau.archs.llm.llm_config import LLMConfig
from nexau.archs.main_sub.agent import Agent
from nexau.archs.main_sub.execution.llm_caller import LLMCaller
from nexau.archs.session.agent_run_action_service import AgentRunActionService
from nexau.archs.tool.tool import Tool
from nexau.archs.tracer.core import BaseTracer, Span, SpanType
from nexau.core.messages import Message

from .history import NexAUHistoryCompatibilityAdapter
from .providers import NexAUProviderAdapter
from .tool import NexAUToolAdapter

_ConfigT = TypeVar("_ConfigT")


@dataclass(frozen=True, slots=True)
class LegacyAgentErrorHandler:
    """Restore the private Agent error-history contract on the public runtime."""

    original: Callable[[Exception, Agent, dict[str, Any]], Any] | None = None

    def __call__(self, error: Exception, agent: Agent, context: dict[str, Any]) -> Any:
        if self.original is None:
            agent.history.append(Message.assistant(f"Error: {error}"))
            agent.history.flush()
            raise error
        response = self.original(error, agent, context)
        agent.history.append(Message.assistant(response))
        agent.history.flush()
        return response


class BorrowedTracer(BaseTracer):
    """Delegate tracing while retaining private non-owning cleanup semantics."""

    def __init__(self, delegate: BaseTracer) -> None:
        self.delegate = delegate

    def start_span(
        self,
        name: str,
        span_type: SpanType,
        inputs: dict[str, Any] | None = None,
        parent_span: Span | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> Span:
        return self.delegate.start_span(name, span_type, inputs, parent_span, attributes)

    def end_span(
        self,
        span: Span,
        outputs: Any = None,
        error: Exception | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        self.delegate.end_span(span, outputs, error, attributes)

    def flush(self) -> None:
        self.delegate.flush()

    def shutdown(self) -> None:
        """Intentionally retain ownership at the NorthCUA application layer."""

    def set_session_id(self, session_id: str) -> None:
        self.delegate.set_session_id(session_id)

    def activate_span(self, span: Span) -> Any | None:
        return self.delegate.activate_span(span)

    def deactivate_span(self, token: Any | None) -> None:
        self.delegate.deactivate_span(token)

    def __getattr__(self, name: str) -> Any:
        return getattr(self.delegate, name)


class NexAURuntimeCapabilityError(RuntimeError):
    """Raised when the pinned public Agent construction contract drifts."""


class NexAURuntimeAdapter:
    """Build extension kwargs without changing the private runtime surface."""

    _creation_hook_lock = RLock()
    _creation_hook_marker = "__northcua_creation_hook_installed__"
    _finalized_marker = "__northcua_runtime_compat_finalized__"

    def __init__(self) -> None:
        self._uses_run_lifecycle_actions = hasattr(AgentRunActionService, "persist_run_start")
        self._provider_adapter = NexAUProviderAdapter()

    def prepare_agent_config(self, config: _ConfigT) -> _ConfigT:
        """Apply public-only runtime shims recursively without forking NexAU types."""

        self._prepare_provider_config_recursive(config)
        if not self._uses_run_lifecycle_actions:
            return config
        self._prepare_agent_config_recursive(config)
        return config

    def install_agent_creation_hook(self) -> None:
        """Finalize every NexAU Agent created below the NorthCUA root boundary.

        Public NexAU constructs subagents inside ``SubAgentManager`` through
        local imports of ``Agent``.  A root-only post-construction pass cannot
        reach those sync, async, resume, or team-created children.  Installing
        one idempotent hook on the reviewed Agent class keeps the compatibility
        behavior at the dependency boundary without copying upstream manager
        control flow.

        ``Agent.create`` invokes ``Agent.__init__`` while async session state is
        intentionally incomplete, so the sync wrapper defers its pass whenever
        NexAU's create flag is active.  The async wrapper finalizes only after
        the upstream factory has rebuilt the executor and transient state.
        """

        with self._creation_hook_lock:
            if getattr(Agent, self._creation_hook_marker, False):
                return

            adapter = self
            original_init = Agent.__init__
            original_create_descriptor = Agent.__dict__.get("create")
            if not isinstance(original_create_descriptor, classmethod):
                raise NexAURuntimeCapabilityError("Agent.create is not the reviewed async classmethod")
            original_create = original_create_descriptor.__func__

            @wraps(original_init)
            def compatible_init(instance: Agent, *args: Any, **kwargs: Any) -> None:
                original_init(instance, *args, **kwargs)
                skip_sync_check = getattr(type(instance), "_is_skip_sync_session_init", None)
                if callable(skip_sync_check) and bool(skip_sync_check()):
                    return
                adapter.finalize_created_agent(instance)

            @wraps(original_create)
            async def compatible_create(agent_class: type[Agent], *args: Any, **kwargs: Any) -> Agent:
                instance = await original_create(agent_class, *args, **kwargs)
                return adapter.finalize_created_agent(instance)

            Agent.__init__ = compatible_init  # type: ignore[method-assign]
            Agent.create = classmethod(compatible_create)  # type: ignore[method-assign]
            setattr(Agent, self._creation_hook_marker, True)

    def _prepare_provider_config_recursive(self, config: object) -> None:
        """Pin provider flags on every root, fallback, and subagent config.

        Config loading prepares the primary model once, but runtime candidate
        cloning and non-inheriting fallback definitions can construct a fresh
        ``LLMConfig`` afterwards. Reapplying the immutable compatibility
        profile here keeps public-only flags explicit at the final Agent
        boundary instead of relying on upstream defaults.
        """

        llm_config = getattr(config, "llm_config", None)
        if isinstance(llm_config, LLMConfig):
            setattr(config, "llm_config", self._provider_adapter.prepare_llm_config(llm_config))
        sub_agents = getattr(config, "sub_agents", None)
        if isinstance(sub_agents, dict):
            for sub_config in sub_agents.values():
                if sub_config is not None:
                    self._prepare_provider_config_recursive(sub_config)

    @classmethod
    def _prepare_agent_config_recursive(cls, config: object) -> None:
        current_handler = getattr(config, "error_handler", None)
        if not isinstance(current_handler, LegacyAgentErrorHandler):
            setattr(config, "error_handler", LegacyAgentErrorHandler(current_handler))
        sub_agents = getattr(config, "sub_agents", None)
        if isinstance(sub_agents, dict):
            for sub_config in sub_agents.values():
                if sub_config is not None:
                    cls._prepare_agent_config_recursive(sub_config)

    def finalize_created_agent(self, agent: Agent) -> Agent:
        """Apply legacy policies to tools NexAU constructs inside ``Agent.create``.

        Config-projected NorthCUA tools already use ``LegacyCompatibleTool``.
        This pass covers MCP, LoadSkill, ToolSearch, and other builtin tools
        without replacing their concrete NexAU subclasses.
        """

        if getattr(agent, self._finalized_marker, False):
            return agent

        NexAUHistoryCompatibilityAdapter.install()
        registry = self._required_attribute(agent, "_tool_registry", owner="Agent")
        get_all = self._required_callable(registry, "get_all", owner="Agent._tool_registry")
        tools = get_all()
        if not isinstance(tools, Mapping) or not all(isinstance(tool, Tool) for tool in tools.values()):
            raise NexAURuntimeCapabilityError("Agent._tool_registry.get_all() returned an unreviewed tool contract")
        for tool in cast(Mapping[str, Tool], tools).values():
            NexAUToolAdapter.enforce_legacy_formatter(tool)

        build_payload = self._required_callable(agent, "_build_tool_call_payload", owner="Agent")
        executor = self._required_attribute(agent, "executor", owner="Agent")
        update_tools = self._required_callable(executor, "update_structured_tools", owner="Agent.executor")
        update_tools(build_payload())

        llm_caller = getattr(executor, "llm_caller", None)
        if not isinstance(llm_caller, LLMCaller):
            raise NexAURuntimeCapabilityError("Agent.executor.llm_caller is not the reviewed NexAU LLMCaller")
        self._provider_adapter.install_on_caller(llm_caller)
        if self._uses_run_lifecycle_actions:
            storage = self._required_attribute(agent, "global_storage", owner="Agent")
            get_storage = self._required_callable(storage, "get", owner="Agent.global_storage")
            set_storage = self._required_callable(storage, "set", owner="Agent.global_storage")
            tracer = get_storage("tracer")
            if isinstance(tracer, BaseTracer) and not isinstance(tracer, BorrowedTracer):
                set_storage("tracer", BorrowedTracer(tracer))
        setattr(agent, self._finalized_marker, True)
        return agent

    @staticmethod
    def _required_attribute(target: object, name: str, *, owner: str) -> object:
        value = getattr(target, name, None)
        if value is None:
            raise NexAURuntimeCapabilityError(f"{owner} has no reviewed {name} boundary")
        return value

    @classmethod
    def _required_callable(cls, target: object, name: str, *, owner: str) -> Callable[..., Any]:
        value = cls._required_attribute(target, name, owner=owner)
        if not callable(value):
            raise NexAURuntimeCapabilityError(f"{owner}.{name} is not callable")
        return cast(Callable[..., Any], value)


__all__ = [
    "BorrowedTracer",
    "LegacyAgentErrorHandler",
    "NexAURuntimeAdapter",
    "NexAURuntimeCapabilityError",
]
