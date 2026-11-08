"""Observation recovery policy and service for Web Agent tool results."""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from time import time_ns

from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.app.logging import log_runtime_event
from northcua.core.json_types import JsonObject
from northcua.core.models import ToolExecutionRecord

from ..domain.actions import WebAgentActionType
from ..domain.observation_recovery import (
    WebObservationRecoveryInvariantError,
    WebObservationRecoveryMode,
    WebObservationRecoveryPlan,
    WebObservationRecoveryReason,
    WebObservationRecoveryResult,
    WebObservationRecoveryStatus,
)
from ..domain.observations import WebObservationContext
from ..mcp.action_service import WebAgentMCPActionService
from ..session import RegisteredWebAgentSession
from .action_resolution import ResolvedWebAgentAction

logger = logging.getLogger(__name__)

_REFRESHED_OBSERVATION_HINT = (
    "The current page observation and actionable refs are attached below. Use only these latest refs."
)
_UNAVAILABLE_OBSERVATION_HINT = "Current browser state could not be collected. Call web_snapshot before using any ref."

_READ_ONLY_ACTIONS = frozenset(
    {
        WebAgentActionType.NOTE,
        WebAgentActionType.SNAPSHOT,
        WebAgentActionType.GET_TEXT,
        WebAgentActionType.GET_URL,
        WebAgentActionType.SCREENSHOT,
        WebAgentActionType.LIST_TABS,
    }
)
_CONTRACT_ERROR_CODES = frozenset(
    {
        "invalid_params",
        "invalid_url_scheme",
        "invalid_wait_target_combination",
        "point_out_of_bounds",
        "policy_or_artifact_error",
        "tab_not_found",
    }
)
_ACTIONABILITY_ERROR_CODES = frozenset(
    {
        "element_disabled",
        "element_not_interactable",
        "element_not_receiving_events",
        "wrong_action_for_element",
    }
)
_FRESH_STATE_ERROR_CODES = frozenset(
    {
        "browser_timeout",
        "missing_ref",
        "post_action_observation_failed",
        "stale_ref",
    }
)


class WebObservationRecoveryPolicy:
    """Choose recovery behavior without collecting or mutating browser state."""

    def plan(
        self,
        *,
        actions: Sequence[ResolvedWebAgentAction],
    ) -> WebObservationRecoveryPlan:
        """Return the recovery plan for a completed command batch."""

        latest_observation_index = self._latest_usable_observation_index(actions)
        trailing_actions = actions[latest_observation_index + 1 :] if latest_observation_index is not None else actions
        refresh_plan = self._latest_refresh_plan(trailing_actions)
        if latest_observation_index is not None and refresh_plan is None:
            return WebObservationRecoveryPlan(
                mode=WebObservationRecoveryMode.USE_POST_ACTION,
                reason=WebObservationRecoveryReason.POST_ACTION_AVAILABLE,
            )
        if not actions:
            return self._reuse_current()
        if refresh_plan is not None:
            return refresh_plan
        return self._reuse_current(target=actions[-1])

    def _latest_refresh_plan(
        self,
        actions: Sequence[ResolvedWebAgentAction],
    ) -> WebObservationRecoveryPlan | None:
        """Return the plan for the chronologically latest refresh trigger."""

        for action in reversed(actions):
            if self._has_unrecovered_observation_error(action):
                return self._refresh(
                    target=action,
                    reason=WebObservationRecoveryReason.POST_ACTION_OBSERVATION_FAILED,
                    trigger_code="post_action_observation_failed",
                )
            if self._is_uncertain_error(action):
                return self._refresh(
                    target=action,
                    reason=self._uncertain_reason(action),
                    trigger_code=self._error_code(action),
                )
            if not action.is_error and action.action not in _READ_ONLY_ACTIONS:
                return self._refresh(
                    target=action,
                    reason=WebObservationRecoveryReason.MUTATION_WITHOUT_OBSERVATION,
                )
        return None

    def _latest_usable_observation_index(self, actions: Sequence[ResolvedWebAgentAction]) -> int | None:
        for index in range(len(actions) - 1, -1, -1):
            output = self._output(actions[index])
            if isinstance(output.get("observation"), Mapping) and output.get("observation"):
                if not self._has_unrecovered_observation_error(actions[index]):
                    return index
        return None

    def _has_unrecovered_observation_error(self, action: ResolvedWebAgentAction) -> bool:
        observation_error = self._output(action).get("observation_error")
        return isinstance(observation_error, Mapping) and not bool(observation_error.get("recovered"))

    def _is_uncertain_error(self, action: ResolvedWebAgentAction) -> bool:
        if not action.is_retryable_error:
            return False
        code = self._error_code(action)
        if code in _CONTRACT_ERROR_CODES:
            return False
        if code in _ACTIONABILITY_ERROR_CODES and self._feedback_source(action) in {
            "contract_validator",
            "preflight",
        }:
            return False
        return code in _FRESH_STATE_ERROR_CODES or action.action not in _READ_ONLY_ACTIONS

    def _uncertain_reason(self, action: ResolvedWebAgentAction) -> WebObservationRecoveryReason:
        if self._error_code(action) in {"missing_ref", "stale_ref"}:
            return WebObservationRecoveryReason.STALE_OR_MISSING_REF
        return WebObservationRecoveryReason.ACTION_OUTCOME_UNCERTAIN

    def _refresh(
        self,
        *,
        target: ResolvedWebAgentAction,
        reason: WebObservationRecoveryReason,
        trigger_code: str = "",
    ) -> WebObservationRecoveryPlan:
        return WebObservationRecoveryPlan(
            mode=WebObservationRecoveryMode.REFRESH_AND_ATTACH,
            reason=reason,
            target_tool_call_id=target.tool_call_id,
            trigger_code=trigger_code,
        )

    def _reuse_current(
        self,
        *,
        target: ResolvedWebAgentAction | None = None,
    ) -> WebObservationRecoveryPlan:
        return WebObservationRecoveryPlan(
            mode=WebObservationRecoveryMode.REUSE_CURRENT,
            reason=WebObservationRecoveryReason.PAGE_STATE_UNCHANGED,
            target_tool_call_id=target.tool_call_id if target is not None else "",
            trigger_code=self._error_code(target) if target is not None else "",
        )

    @staticmethod
    def _output(action: ResolvedWebAgentAction) -> JsonObject:
        return action.tool_output if isinstance(action.tool_output, dict) else {}

    def _error_code(self, action: ResolvedWebAgentAction) -> str:
        output = self._output(action)
        return str(output.get("reason_code") or output.get("code") or "").strip()

    def _feedback_source(self, action: ResolvedWebAgentAction) -> str:
        return str(self._output(action).get("feedback_source") or "").strip()


class WebObservationRecoveryService:
    """Collect a necessary recovery observation and attach it to a real call."""

    def __init__(
        self,
        *,
        action_service: WebAgentMCPActionService,
        policy: WebObservationRecoveryPolicy | None = None,
    ) -> None:
        self.action_service = action_service
        self.policy = policy or WebObservationRecoveryPolicy()

    async def recover(
        self,
        *,
        session: RegisteredWebAgentSession,
        trace_state: TraceCaptureState,
        actions: Sequence[ResolvedWebAgentAction],
    ) -> WebObservationRecoveryResult:
        """Apply policy and enrich the existing real tool result when needed."""

        plan = self.policy.plan(actions=actions)
        if plan.mode is not WebObservationRecoveryMode.REFRESH_AND_ATTACH:
            self._log_decision(session=session, plan=plan, recovered=False)
            return WebObservationRecoveryResult(plan=plan)

        target_execution = self._target_execution(
            trace_state.tool_executions,
            tool_call_id=plan.target_tool_call_id,
        )
        if target_execution is None:
            error = f"real tool result {plan.target_tool_call_id!r} was not found"
            session.clear_ref_aliases(observation_id=f"recovery-failed-{time_ns()}")
            self._log_decision(session=session, plan=plan, recovered=False, error=error)
            raise WebObservationRecoveryInvariantError(error)

        mode = session.settings.post_action_observation_mode
        if mode == "none":
            mode = "snapshot"
        try:
            observation = await self.action_service.collect_current_observation(
                session,
                mode=mode,
                observation_id=f"recovery-{session.session_id}-{time_ns()}",
                validate_url=True,
                publish_ref_aliases=False,
            )
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            session.clear_ref_aliases(observation_id=f"recovery-failed-{time_ns()}")
            self._attach_recovery_failure(
                trace_state=trace_state,
                plan=plan,
                error=error,
                discard_observation=True,
            )
            self._log_decision(
                session=session,
                plan=plan,
                recovered=False,
                error=error,
            )
            return WebObservationRecoveryResult(
                plan=plan,
                error=error,
                refs_invalidated=True,
            )
        self._attach_to_real_tool_result(
            execution=target_execution,
            plan=plan,
            observation=observation,
        )
        try:
            self.action_service.update_ref_aliases_from_observation(session, observation)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            session.clear_ref_aliases(observation_id=f"recovery-failed-{time_ns()}")
            self._attach_recovery_failure(
                trace_state=trace_state,
                plan=plan,
                error=error,
                discard_observation=True,
            )
            self._log_decision(session=session, plan=plan, recovered=False, error=error)
            return WebObservationRecoveryResult(
                plan=plan,
                error=error,
                refs_invalidated=True,
            )
        self._log_decision(session=session, plan=plan, recovered=True)
        return WebObservationRecoveryResult(plan=plan, recovered=True)

    def _attach_to_real_tool_result(
        self,
        *,
        execution: ToolExecutionRecord,
        plan: WebObservationRecoveryPlan,
        observation: WebObservationContext,
    ) -> None:
        observation_payload, image_urls = self.action_service.project_observation_payload(observation)
        recovery_payload: JsonObject = {
            "status": WebObservationRecoveryStatus.RECOVERED.value,
            "source": "runner_refresh",
            "reason": plan.reason.value,
            "trigger_code": plan.trigger_code,
            "observation_id": observation.observation_id,
        }
        canonical_source = execution.tool_output
        model_source = execution.llm_tool_output if execution.llm_tool_output is not None else canonical_source
        execution.tool_output = self._enriched_output(
            canonical_source,
            observation_payload=observation_payload,
            image_urls=image_urls,
            recovery_payload=recovery_payload,
        )
        execution.llm_tool_output = self._model_enriched_output(
            model_source,
            observation_payload=observation_payload,
            image_urls=image_urls,
        )

    def _attach_recovery_failure(
        self,
        *,
        trace_state: TraceCaptureState,
        plan: WebObservationRecoveryPlan,
        error: str,
        discard_observation: bool = False,
    ) -> None:
        execution = self._target_execution(
            trace_state.tool_executions,
            tool_call_id=plan.target_tool_call_id,
        )
        if execution is None:
            return
        failure: JsonObject = {
            "status": WebObservationRecoveryStatus.FAILED.value,
            "source": "runner_refresh",
            "reason": plan.reason.value,
            "trigger_code": plan.trigger_code,
            "error": error,
            "retryable": True,
            "refs_invalidated": True,
            "hint": _UNAVAILABLE_OBSERVATION_HINT,
        }
        canonical_source = execution.tool_output
        model_source = execution.llm_tool_output if execution.llm_tool_output is not None else canonical_source
        execution.tool_output = self._output_with_recovery_failure(
            canonical_source,
            failure=failure,
            discard_observation=discard_observation,
        )
        execution.llm_tool_output = self._model_output_with_recovery_failure(
            model_source,
            discard_observation=discard_observation,
        )

    @staticmethod
    def without_actionable_refs(observation: WebObservationContext) -> WebObservationContext:
        """Keep safe page metadata while expiring an unusable ref observation."""

        return observation.model_copy(
            update={
                "snapshot": observation.snapshot.model_copy(
                    update={
                        "snapshot": "",
                        "elements": [],
                        "raw": {},
                    }
                ),
                "screenshot": None,
                "settled": False,
                "settle_reason": "observation_recovery_failed",
            }
        )

    @staticmethod
    def _output_with_recovery_failure(
        value: JsonObject | str | None,
        *,
        failure: JsonObject,
        discard_observation: bool = False,
    ) -> JsonObject:
        if isinstance(value, dict):
            payload: JsonObject = dict(value)
        elif isinstance(value, str) and value:
            payload = {"status": "error", "error": value}
        else:
            payload = {"status": "error"}
        if discard_observation:
            WebObservationRecoveryService._discard_unusable_observation(payload)
            observation_error = payload.get("observation_error")
            if isinstance(observation_error, dict) and observation_error.get("recovered") is True:
                payload["observation_error"] = {
                    **observation_error,
                    "recovered": False,
                    "hint": str(failure.get("hint") or ""),
                }
        payload["observation_recovery"] = dict(failure)
        return payload

    @staticmethod
    def _model_output_with_recovery_failure(
        value: JsonObject | str | None,
        *,
        discard_observation: bool = False,
    ) -> JsonObject:
        """Expose the actionable state consequence, not runner internals."""

        if isinstance(value, dict):
            payload: JsonObject = dict(value)
        elif isinstance(value, str) and value:
            payload = {"status": "error", "error": value}
        else:
            payload = {"status": "error"}
        if discard_observation:
            WebObservationRecoveryService._discard_unusable_observation(payload)
        payload.pop("observation_recovery", None)
        payload["observation_error"] = {
            "code": "current_observation_unavailable",
            "retryable": True,
            "refs_invalidated": True,
            "hint": _UNAVAILABLE_OBSERVATION_HINT,
        }
        return payload

    @staticmethod
    def _discard_unusable_observation(payload: JsonObject) -> None:
        """Remove stale current-state channels while preserving action evidence."""

        stale_keys = ("observation", "screenshot_url", "raw_image_url", "som_image_url")
        for key in stale_keys:
            payload.pop(key, None)
        result = payload.get("result")
        if isinstance(result, dict):
            sanitized_result: JsonObject = dict(result)
            for key in stale_keys:
                sanitized_result.pop(key, None)
            payload["result"] = sanitized_result

    def _enriched_output(
        self,
        value: JsonObject | str | None,
        *,
        observation_payload: JsonObject,
        image_urls: JsonObject,
        recovery_payload: JsonObject,
    ) -> JsonObject:
        payload: JsonObject
        if isinstance(value, dict):
            payload = dict(value)
        elif isinstance(value, str) and value:
            payload = {"status": "error", "error": value}
        else:
            payload = {}
        self.action_service.attach_observation_payload(
            payload,
            observation=observation_payload,
            image_urls=image_urls,
        )
        payload["observation_recovery"] = dict(recovery_payload)
        observation_error = payload.get("observation_error")
        if isinstance(observation_error, dict):
            payload["observation_error"] = {
                **observation_error,
                "recovered": True,
                "hint": "A fresh recovery observation is attached to this tool result; use its refs.",
            }
        return payload

    def _model_enriched_output(
        self,
        value: JsonObject | str | None,
        *,
        observation_payload: JsonObject,
        image_urls: JsonObject,
    ) -> JsonObject:
        """Attach fresh state while hiding recovery implementation metadata."""

        if isinstance(value, dict):
            payload: JsonObject = dict(value)
        elif isinstance(value, str) and value:
            payload = {"status": "error", "error": value}
        else:
            payload = {}
        model_observation = dict(observation_payload)
        model_observation.pop("observation_id", None)
        self.action_service.attach_observation_payload(
            payload,
            observation=model_observation,
            image_urls=image_urls,
        )
        payload.pop("observation_recovery", None)
        # Fresh state fully heals an observation-collection error. Preserve the
        # original action status/code/error so the model never mistakes a
        # failed action for a successful one.
        payload.pop("observation_error", None)
        if payload.get("status") == "error":
            existing_hint = str(payload.get("hint") or "").strip()
            payload["hint"] = (
                f"{existing_hint} {_REFRESHED_OBSERVATION_HINT}".strip()
                if existing_hint
                else _REFRESHED_OBSERVATION_HINT
            )
        return payload

    @staticmethod
    def _target_execution(
        executions: Sequence[ToolExecutionRecord],
        *,
        tool_call_id: str,
    ) -> ToolExecutionRecord | None:
        if not tool_call_id:
            return None
        for execution in reversed(executions):
            if execution.tool_call_id == tool_call_id:
                return execution
        return None

    @staticmethod
    def _log_decision(
        *,
        session: RegisteredWebAgentSession,
        plan: WebObservationRecoveryPlan,
        recovered: bool,
        error: str = "",
    ) -> None:
        log_runtime_event(
            logger,
            "web_agent_observation_recovery",
            level=logging.INFO if error else logging.DEBUG,
            session_id=session.session_id,
            recovery_mode=plan.mode.value,
            recovery_reason=plan.reason.value,
            trigger_code=plan.trigger_code,
            target_tool_call_id=plan.target_tool_call_id,
            recovered=recovered,
            error=error,
        )


__all__ = ["WebObservationRecoveryPolicy", "WebObservationRecoveryService"]
