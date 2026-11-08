"""Private-pin history generation policy for public NexAU runtimes."""

from __future__ import annotations

import threading
from collections.abc import Callable
from functools import wraps
from typing import cast

from nexau.archs.main_sub.history_list import HistoryList
from nexau.archs.session.agent_run_action_service import AgentRunActionService
from nexau.core.messages import ToolResultBlock

_RAW_OUTPUT_PATCH_MARKER = "__northcua_legacy_raw_output_policy__"
_HISTORY_FLUSH_PATCH_MARKER = "__northcua_legacy_history_flush_policy__"
_LIFECYCLE_PATCH_MARKER = "__northcua_legacy_lifecycle_policy__"
_INSTALL_LOCK = threading.Lock()


class NexAUHistoryCapabilityError(RuntimeError):
    """Raised when the pinned executor no longer exposes the reviewed boundary."""


class NexAUHistoryCompatibilityAdapter:
    """Disable public-only raw tool output before messages enter history.

    Public ``c98f7fb`` introduced ``ToolResultBlock.raw_output`` in both XML
    and structured execution paths. Private ``58d3fa4`` never created it.
    Installing the policy at their shared construction helper keeps in-memory
    messages, HistoryList fingerprints, session rows, resume, and provider
    replay aligned without maintaining separate persistence projections.
    """

    @classmethod
    def install(cls) -> None:
        """Install the exact private behavior once, failing on API drift."""

        # The isolated private-baseline suite executes the same NorthCUA source
        # with pre-migration NexAU. That runtime already has the desired
        # contract and exposes none of the public-only raw-output surface.
        if "raw_output" not in ToolResultBlock.model_fields:
            return

        import nexau.archs.main_sub.execution.executor as executor_module

        with _INSTALL_LOCK:
            cls._install_history_flush_policy()
            cls._install_lifecycle_policy()
            current = getattr(executor_module, "_coerce_raw_output", None)
            if not callable(current):
                raise NexAUHistoryCapabilityError(
                    "installed NexAU has no reviewed tool raw-output construction boundary"
                )
            if getattr(current, _RAW_OUTPUT_PATCH_MARKER, False):
                return

            def discard_public_raw_output(_output: object) -> None:
                return None

            setattr(discard_public_raw_output, _RAW_OUTPUT_PATCH_MARKER, True)
            executor_module._coerce_raw_output = cast(Callable[[object], None], discard_public_raw_output)

    @staticmethod
    def _install_history_flush_policy() -> None:
        current_flush = HistoryList.flush
        if not getattr(current_flush, _HISTORY_FLUSH_PATCH_MARKER, False):
            original_flush = current_flush

            @wraps(original_flush)
            def legacy_flush(self: HistoryList, *, iter_index: int | None = None) -> None:
                # Public per-iteration writes have no private equivalent. The
                # no-index end/error/finally flush remains the private batch
                # persistence boundary.
                if iter_index is not None:
                    return
                original_flush(self)

            setattr(legacy_flush, _HISTORY_FLUSH_PATCH_MARKER, True)
            HistoryList.flush = legacy_flush

        current_flush_async = HistoryList.flush_async
        if not getattr(current_flush_async, _HISTORY_FLUSH_PATCH_MARKER, False):

            async def legacy_flush_async(self: HistoryList, *, iter_index: int | None = None) -> None:
                # Public Agent.run_async uses this only for the eager user
                # write. Private NexAU deferred it to the batch flush.
                del self, iter_index

            setattr(legacy_flush_async, _HISTORY_FLUSH_PATCH_MARKER, True)
            HistoryList.flush_async = legacy_flush_async

    @staticmethod
    def _install_lifecycle_policy() -> None:
        for name in ("persist_run_start", "persist_run_end"):
            current = getattr(AgentRunActionService, name, None)
            if not callable(current):
                raise NexAUHistoryCapabilityError(f"installed NexAU has no reviewed lifecycle boundary {name}")
            if getattr(current, _LIFECYCLE_PATCH_MARKER, False):
                continue

            async def skip_public_lifecycle_action(
                _service: AgentRunActionService,
                *args: object,
                **kwargs: object,
            ) -> None:
                del args, kwargs

            skip_public_lifecycle_action.__name__ = name
            setattr(skip_public_lifecycle_action, _LIFECYCLE_PATCH_MARKER, True)
            setattr(AgentRunActionService, name, skip_public_lifecycle_action)


__all__ = ["NexAUHistoryCapabilityError", "NexAUHistoryCompatibilityAdapter"]
