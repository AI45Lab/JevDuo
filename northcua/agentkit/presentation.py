"""Terminal presentation helpers for AgentKit agents."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, TypeVar, cast

from northcua.app.terminal_renderer.base import AgentTerminalRendererBase

from .imports import resolve_dotted
from .registry import load_all_agent_specs
from .spec import AgentPresentationSpec, AgentSpec

TResult = TypeVar("TResult")


@dataclass(slots=True, frozen=True)
class AgentRenderContext:
    """Common fields available to AgentKit terminal renderers.

    Attributes:
        agent_kind: Stable AgentKit kind being rendered.
        display_name: Human-readable agent name shown in panel titles.
        session_id: Session identifier for this CLI run.
        log_dir: Artifact directory printed for follow-up inspection.
        query: User query or task summary for the run.
        request: Optional typed request object used by richer agents.
        extras: Additional renderer-specific values passed through unchanged.
    """

    agent_kind: str
    display_name: str
    session_id: str
    log_dir: Path
    query: str
    request: object | None = None
    extras: Mapping[str, object] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class TerminalRunCallbacks:
    """Callbacks passed from CLI run helpers into agent runtimes.

    Attributes:
        on_before_step: Callback invoked before a one-based step starts.
        on_step: Callback invoked with a runtime step or panel record.
    """

    on_before_step: Callable[[int], None]
    on_step: Callable[[object], None]


class AgentTerminalRendererProtocol(Protocol):
    """Protocol implemented by terminal renderers used by AgentKit."""

    def render_header(self, **kwargs: object) -> str:
        """Render a run header.

        Returns:
            Terminal-ready header text for the run.
        """
        ...

    def render_step_start(self, step_index: int) -> str:
        """Render a pre-step banner.

        Args:
            step_index: One-based step index about to execute.

        Returns:
            Terminal-ready text displayed before the step body.
        """
        ...

    def render_step(self, step: object) -> str:
        """Render one step or panel record.

        Args:
            step: Runtime step record after optional projection.

        Returns:
            Terminal-ready panel text for the step.
        """
        ...

    def render_footer(self, **kwargs: object) -> str:
        """Render a run footer.

        Returns:
            Terminal-ready summary text for the completed run.
        """
        ...


class PanelProjectorProtocol(Protocol):
    """Protocol for converting runtime records into renderer-facing panels."""

    def project(self, step: object) -> object:
        """Return the object that should be passed to the renderer.

        Args:
            step: Runtime record emitted by an agent runner.

        Returns:
            Renderer-facing panel record.
        """


class IdentityPanelProjector:
    """Default projector for runtimes that already emit panel-ready records."""

    def project(self, step: object) -> object:
        """Return the step unchanged.

        Args:
            step: Runtime record already shaped for a renderer.

        Returns:
            The original step object.
        """

        return step


class TemplateTerminalRenderer(AgentTerminalRendererBase):
    """Small default renderer for low-code agents without a custom renderer."""

    def __init__(self, spec: AgentSpec | None = None) -> None:
        """Initialize a renderer from optional AgentKit template metadata.

        Args:
            spec: Agent spec whose presentation templates should be used.
        """

        self.spec = spec
        self.templates = dict(spec.presentation.templates) if spec is not None else {}

    def render_header(self, **kwargs: object) -> str:
        """Render a generic AgentKit header.

        Args:
            **kwargs: Renderer context fields, usually including ``context``.

        Returns:
            Panel text containing agent, session, artifact directory, and query.
        """

        context = _context_from_kwargs(kwargs)
        lines = [
            f"Agent: {context.display_name}",
            f"Session: {context.session_id}",
            f"Trace Dir: {context.log_dir}",
            f"Query: {context.query}",
        ]
        return self._panel(
            title=f"{context.display_name} Run", lines=self._section_lines("Run", lines, color=self.blue)
        )

    def render_step(self, step: object) -> str:
        """Render a generic step record.

        Args:
            step: Mapping, dataclass, Pydantic model, or primitive step payload.

        Returns:
            Panel text with step index, status, and summary when available.
        """

        payload = _json_safe(step)
        if isinstance(payload, Mapping):
            status = str(payload.get("status") or payload.get("terminal_status") or "ok")
            step_index = payload.get("step") or payload.get("step_index") or payload.get("session_step") or "?"
            summary = str(payload.get("summary") or payload.get("action") or payload.get("assistant_text") or "")
            lines = self._section_lines(
                "Step", [f"Status: {status}", f"Summary: {summary or '(none)'}"], color=self.cyan
            )
            return self._panel(title=f"Step {step_index} [{self._status_label(status)}]", lines=lines)
        lines = self._section_lines("Step", [str(payload)], color=self.cyan)
        return self._panel(title="Step", lines=lines)

    def render_footer(self, **kwargs: object) -> str:
        """Render a generic AgentKit footer.

        Args:
            **kwargs: Renderer context fields plus optional ``result``.

        Returns:
            Panel text summarizing artifacts and typed result fields.
        """

        context = _context_from_kwargs(kwargs)
        result = kwargs.get("result")
        result_payload = _json_safe(result)
        lines = [f"Trace Dir: {context.log_dir}"]
        if isinstance(result_payload, Mapping):
            for key in ("status", "terminal_status", "success", "step_count", "primary_result_path"):
                if key in result_payload:
                    lines.append(f"{key}: {result_payload[key]}")
            terminal_decision = result_payload.get("terminal_decision")
            if terminal_decision:
                lines.extend(self._terminal_decision_lines(terminal_decision))
        else:
            lines.append(f"Result: {result_payload}")
        return self._panel(
            title=f"{context.display_name} Summary",
            lines=self._section_lines("Summary", lines, color=self.blue),
        )


def resolve_terminal_renderer(agent_kind: str) -> AgentTerminalRendererProtocol:
    """Instantiate the terminal renderer declared by an AgentKit spec.

    Args:
        agent_kind: Registered AgentKit kind whose renderer should be loaded.

    Returns:
        Renderer instance, falling back to ``TemplateTerminalRenderer``.
    """

    spec = load_all_agent_specs().get(agent_kind)
    renderer_cls = _resolve_optional(spec.presentation.renderer_cls) or TemplateTerminalRenderer
    try:
        return cast(AgentTerminalRendererProtocol, cast(Callable[[AgentSpec], object], renderer_cls)(spec))
    except TypeError:
        return cast(AgentTerminalRendererProtocol, cast(Callable[[], object], renderer_cls)())


def resolve_panel_projector(agent_kind: str) -> PanelProjectorProtocol:
    """Instantiate the panel projector declared by an AgentKit spec.

    Args:
        agent_kind: Registered AgentKit kind whose projector should be loaded.

    Returns:
        Projector instance, falling back to ``IdentityPanelProjector``.
    """

    spec = load_all_agent_specs().get(agent_kind)
    projector_cls = _resolve_optional(spec.presentation.panel_projector_cls) or IdentityPanelProjector
    try:
        return cast(PanelProjectorProtocol, cast(Callable[[AgentSpec], object], projector_cls)(spec))
    except TypeError:
        return cast(PanelProjectorProtocol, cast(Callable[[], object], projector_cls)())


async def run_with_terminal_renderer(
    *,
    context: AgentRenderContext,
    renderer: AgentTerminalRendererProtocol,
    run: Callable[[TerminalRunCallbacks], Awaitable[TResult]],
    footer_fields: Callable[[TResult], Mapping[str, object]] | None = None,
    projector: PanelProjectorProtocol | None = None,
    emit: Callable[[str], None] | None = None,
) -> TResult:
    """Run an agent task while rendering standardized terminal panels.

    Args:
        context: Shared terminal context for header, footer, and callbacks.
        renderer: Terminal renderer resolved from the agent spec.
        run: Coroutine function that executes the agent and receives callbacks.
        footer_fields: Optional projector for result-specific footer fields.
        projector: Optional runtime-step to panel-record projector.
        emit: Optional sink for rendered text; defaults to ``print``.

    Returns:
        The typed result returned by ``run``.
    """

    writer = emit or _default_emit
    active_projector = projector or IdentityPanelProjector()
    writer(_call_renderer(renderer.render_header, {"context": context, **context.extras}))

    def _on_before_step(step_index: int) -> None:
        writer(renderer.render_step_start(step_index))

    def _on_step(step: object) -> None:
        writer(renderer.render_step(active_projector.project(step)))

    result = await run(TerminalRunCallbacks(on_before_step=_on_before_step, on_step=_on_step))
    footer_kwargs: dict[str, object] = {"context": context, "result": result, **context.extras}
    if footer_fields is not None:
        footer_kwargs.update(footer_fields(result))
    writer(_call_renderer(renderer.render_footer, footer_kwargs))
    return result


def presentation_spec(
    *,
    renderer_cls: str | type[object] | None = None,
    panel_projector_cls: str | type[object] | None = None,
    templates: Mapping[str, str] | None = None,
) -> AgentPresentationSpec:
    """Convenience constructor for concise low-code specs.

    Args:
        renderer_cls: Optional renderer class or import path.
        panel_projector_cls: Optional projector class or import path.
        templates: Optional template mapping for the default renderer.

    Returns:
        Presentation spec ready to attach to an ``AgentSpec``.
    """

    return AgentPresentationSpec(
        renderer_cls=renderer_cls,
        panel_projector_cls=panel_projector_cls,
        templates=dict(templates or {}),
    )


def _resolve_optional(value: str | type[object] | None) -> type[object] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return cast(type[object], resolve_dotted(value))
    return value


def _call_renderer(method: Callable[..., str], kwargs: Mapping[str, object]) -> str:
    signature = inspect.signature(method)
    if any(parameter.kind == parameter.VAR_KEYWORD for parameter in signature.parameters.values()):
        return method(**dict(kwargs))
    filtered = {key: value for key, value in kwargs.items() if key in signature.parameters}
    return method(**filtered)


def _context_from_kwargs(kwargs: Mapping[str, object]) -> AgentRenderContext:
    context = kwargs.get("context")
    if isinstance(context, AgentRenderContext):
        return context
    return AgentRenderContext(
        agent_kind=str(kwargs.get("agent_kind") or "agent"),
        display_name=str(kwargs.get("display_name") or "AgentKit Agent"),
        session_id=str(kwargs.get("session_id") or ""),
        log_dir=Path(str(kwargs.get("log_dir") or ".")),
        query=str(kwargs.get("query") or ""),
        request=kwargs.get("request"),
    )


def _json_safe(value: object) -> object:
    if hasattr(value, "model_dump"):
        return cast(Any, value).model_dump()
    if hasattr(value, "__dataclass_fields__"):
        from dataclasses import asdict

        return asdict(cast(Any, value))
    try:
        return json.loads(json.dumps(value, default=str))
    except TypeError:
        return str(value)


def _default_emit(text: str) -> None:
    print(text, flush=True)
