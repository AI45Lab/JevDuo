"""Fail-closed budget validation at NexAU's final provider boundary."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from functools import wraps
from typing import cast

from nexau.archs.main_sub.execution.hooks import ModelCallParams
from nexau.core.messages import Message

from northcua.agentkernel.context.strategies import ContextBudgetExceededError

from .ump import NexAUTokenEstimator


class ProviderContextBudgetGuard:
    """Validate provider-ready messages and tools immediately before dispatch."""

    def __init__(self, *, model: str) -> None:
        self._estimator = NexAUTokenEstimator(model=model)
        self._hard_input_budget: int | None = None
        self._tools: tuple[Mapping[str, object], ...] = ()

    def configure(self, *, hard_input_budget: int, tools: Sequence[Mapping[str, object]]) -> None:
        if hard_input_budget <= 0:
            raise ValueError("provider context hard input budget must be positive")
        self._hard_input_budget = hard_input_budget
        self._tools = tuple(tools)

    def validate(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, object]] | None = None,
    ) -> None:
        budget = self._hard_input_budget
        if budget is None:
            return
        estimate = self._estimator.estimate(tuple(messages), tuple(tools) if tools is not None else self._tools)
        if estimate.total_tokens > budget:
            raise ContextBudgetExceededError(
                "final provider context exceeds safe input budget at provider call boundary "
                f"({estimate.total_tokens}>{budget})"
            )


class _ProviderBudgetAbort(BaseException):
    """Cross NexAU's retry loop without being treated as a provider failure."""

    def __init__(self, error: ContextBudgetExceededError) -> None:
        super().__init__(str(error))
        self.error = error


@dataclass(slots=True)
class _GuardedCallerState:
    guard: ProviderContextBudgetGuard


def _provider_tools(params: ModelCallParams) -> Sequence[Mapping[str, object]] | None:
    raw_tools = params.api_params.get("tools")
    if (
        isinstance(raw_tools, Sequence)
        and not isinstance(raw_tools, (str, bytes))
        and all(isinstance(tool, Mapping) for tool in raw_tools)
    ):
        return cast(Sequence[Mapping[str, object]], raw_tools)
    return None


def _validate_params(state: _GuardedCallerState, params: ModelCallParams) -> None:
    state.guard.validate(params.messages, _provider_tools(params))


def install_provider_context_budget_guard(caller: object, guard: ProviderContextBudgetGuard) -> None:
    """Install a final-boundary guard for the pinned NexAU ``LLMCaller``.

    With middleware, NexAU performs retries *outside* ``wrap_model_call``.  The
    terminal wrapper therefore uses a private ``BaseException`` sentinel so a
    deterministic budget rejection is neither swallowed nor retried, and the
    public call wrapper converts it back to the typed domain error.  Without
    middleware, the normalized retry entrypoint is already the final immutable
    boundary (including Gemini REST), so it can be guarded directly.
    """

    existing = getattr(caller, "__northcua_context_guard_state__", None)
    if isinstance(existing, _GuardedCallerState):
        existing.guard = guard
        return
    state = _GuardedCallerState(guard=guard)
    manager = getattr(caller, "middleware_manager", None)
    wrap_model_call = getattr(manager, "wrap_model_call", None)
    if manager and callable(wrap_model_call):
        original_manager_wrap = cast(Callable[..., object], wrap_model_call)

        @wraps(original_manager_wrap)
        def guarded_manager_wrap(
            params: ModelCallParams,
            call_next: Callable[[ModelCallParams], object],
        ) -> object:
            def final_call(final_params: ModelCallParams) -> object:
                try:
                    _validate_params(state, final_params)
                except ContextBudgetExceededError as exc:
                    raise _ProviderBudgetAbort(exc) from None
                return call_next(final_params)

            return original_manager_wrap(params, final_call)

        setattr(manager, "wrap_model_call", guarded_manager_wrap)
        _wrap_public_call(caller, "call_llm", state)
        _wrap_public_call(caller, "call_llm_async", state)
    else:
        _wrap_retry_boundary(caller, "_call_with_retry", state)
        _wrap_async_retry_boundary(caller, "_call_with_retry_async", state)
    setattr(caller, "__northcua_context_guard_state__", state)


def _wrap_public_call(caller: object, name: str, state: _GuardedCallerState) -> None:
    del state  # state is captured by the terminal manager wrapper.
    original = getattr(caller, name, None)
    if not callable(original):
        raise TypeError(f"prepared NexAU caller has no {name} boundary")
    if name.endswith("_async"):
        typed_original = cast(Callable[..., Awaitable[object]], original)

        @wraps(typed_original)
        async def guarded_async(*args: object, **kwargs: object) -> object:
            try:
                return await typed_original(*args, **kwargs)
            except _ProviderBudgetAbort as abort:
                raise abort.error from None

        setattr(caller, name, guarded_async)
        return
    typed_sync = cast(Callable[..., object], original)

    @wraps(typed_sync)
    def guarded_sync(*args: object, **kwargs: object) -> object:
        try:
            return typed_sync(*args, **kwargs)
        except _ProviderBudgetAbort as abort:
            raise abort.error from None

    setattr(caller, name, guarded_sync)


def _wrap_retry_boundary(caller: object, name: str, state: _GuardedCallerState) -> None:
    original = getattr(caller, name, None)
    if not callable(original):
        raise TypeError(f"prepared NexAU caller has no normalized {name} boundary")
    typed_original = cast(Callable[..., object], original)

    @wraps(typed_original)
    def guarded(params: ModelCallParams, *args: object, **kwargs: object) -> object:
        _validate_params(state, params)
        return typed_original(params, *args, **kwargs)

    setattr(caller, name, guarded)


def _wrap_async_retry_boundary(caller: object, name: str, state: _GuardedCallerState) -> None:
    original = getattr(caller, name, None)
    if not callable(original):
        raise TypeError(f"prepared NexAU caller has no normalized {name} boundary")
    typed_original = cast(Callable[..., Awaitable[object]], original)

    @wraps(typed_original)
    async def guarded(params: ModelCallParams, *args: object, **kwargs: object) -> object:
        _validate_params(state, params)
        return await typed_original(params, *args, **kwargs)

    setattr(caller, name, guarded)
