"""Shared execution safety primitives for context-managed provider fallback."""

from __future__ import annotations

from dataclasses import dataclass

from nexau.archs.main_sub.execution.hooks import BeforeToolHookInput, HookResult

from northcua.agentkernel.context.domain.enums import ContextManagementMode
from northcua.agentkernel.context.strategies import ContextSideEffectReplayError


@dataclass(slots=True)
class ContextSideEffectFence:
    """Prevent a fresh provider candidate from replaying executed tools."""

    mode: ContextManagementMode
    started: bool = False

    def before_tool(self, hook_input: BeforeToolHookInput) -> HookResult:
        del hook_input
        if self.mode == ContextManagementMode.ENFORCE:
            self.started = True
        return HookResult.no_changes()

    def mark_external_tool_dispatch(self) -> None:
        if self.mode == ContextManagementMode.ENFORCE:
            self.started = True

    def raise_if_candidate_replay_unsafe(self, error: Exception) -> None:
        if self.mode == ContextManagementMode.ENFORCE and self.started:
            raise ContextSideEffectReplayError(
                "provider fallback is disabled after tool side effects have started"
            ) from error
