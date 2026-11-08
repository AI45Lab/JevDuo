"""Trace persistence helpers for AgentKit evaluator runs."""

from __future__ import annotations

import json

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.core.enums import EvaluationToolCheckStatus
from northcua.core.models import LLMTraceRecord, ToolExecutionRecord
from northcua.core.terminal import AgentTerminalDecision

from ..artifacts import EvaluatorArtifactWriter
from ..protocol import EvaluationToolCheck


class EvaluatorTraceWriter:
    """Persist evaluator LLM/tool traces while protecting sensitive keys."""

    def __init__(self, artifact_writer: EvaluatorArtifactWriter | None) -> None:
        """Create a writer backed by an optional structured artifact writer.

        Args:
            artifact_writer: Structured artifact writer, or ``None`` to disable persistence.
        """

        self.artifact_writer = artifact_writer

    def persist_step_trace(
        self,
        *,
        session_id: str | None = None,
        round_num: int,
        evaluator_step: int,
        request: str | None = None,
        trace_state: TraceCaptureState,
        duration_ms: float,
    ) -> None:
        """Persist one AgentKit evaluator model/tool step trace.

        Args:
            session_id: Optional session id from newer evaluator runtimes.
            round_num: Evaluated round number.
            evaluator_step: Evaluator loop step number.
            request: Optional round request from newer evaluator runtimes.
            trace_state: Captured model and tool trace state.
            duration_ms: Step wall-clock duration in milliseconds.
        """

        if self.artifact_writer is None:
            return
        tool_checks = [self.tool_check_from_execution(execution) for execution in trace_state.tool_executions]
        self.artifact_writer.append_llm_trace(
            round_num=round_num,
            payload=self.full_trace_payload(
                session_id=session_id,
                round_num=round_num,
                evaluator_step=evaluator_step,
                request=request,
                trace_state=trace_state,
                tool_checks=tool_checks,
            ),
        )
        trace_state.release_provider_payloads()
        self.artifact_writer.append_runtime_event(
            round_num=round_num,
            payload={
                "event": "agentkit_evaluator_step_completed",
                "runtime_phase": "evaluator",
                "phase_step": evaluator_step,
                "step": evaluator_step,
                "duration_ms": round(duration_ms, 2),
                "tool_call_count": len(trace_state.assistant_tool_calls),
                "tool_execution_count": len(trace_state.tool_executions),
                "stop_reason": trace_state.stop_reason,
                "cost": trace_state.cost,
            },
        )

    def full_trace_payload(
        self,
        *,
        session_id: str | None,
        round_num: int,
        evaluator_step: int,
        request: str | None,
        trace_state: TraceCaptureState,
        tool_checks: list[EvaluationToolCheck],
    ) -> JsonObject:
        """Build a main-agent-compatible evaluator LLM trace row.

        Args:
            session_id: Optional evaluator session id.
            round_num: Evaluated round number.
            evaluator_step: Evaluator loop step number.
            request: Optional evaluated round request.
            trace_state: Captured model/tool trace state.
            tool_checks: Public tool check records derived from executions.

        Returns:
            JSON payload persisted as an evaluator LLM trace row.
        """

        llm_trace = LLMTraceRecord(
            request=self.redact_sensitive_json_object(trace_state.request_payload),
            response=self.redact_sensitive_json_object(trace_state.response_payload),
            usage=trace_state.usage,
            stop_reason=trace_state.stop_reason,
            cost=trace_state.cost,
            reasoning=trace_state.reasoning,
            reasoning_encrypted_content=trace_state.reasoning_encrypted_content,
            assistant_text=trace_state.assistant_text,
            assistant_tool_calls=list(trace_state.assistant_tool_calls),
            tool_executions=list(trace_state.tool_executions),
        )
        return {
            "source": "evaluator",
            "session_id": session_id or "",
            "round_num": round_num,
            "evaluator_step": evaluator_step,
            "phase": f"agentkit_step_{evaluator_step}",
            "request": request or "",
            "usage": dict(trace_state.usage),
            "stop_reason": trace_state.stop_reason,
            "cost": trace_state.cost,
            "tool_checks": [check.model_dump(mode="json") for check in tool_checks],
            "llm_trace": llm_trace.model_dump(mode="json"),
        }

    def persist_tool_traces(
        self,
        *,
        round_num: int,
        tool_executions: list[ToolExecutionRecord],
    ) -> None:
        """Persist evaluator tool execution summaries.

        Args:
            round_num: Evaluated round number.
            tool_executions: Tool execution records captured for one evaluator step.
        """

        if self.artifact_writer is None:
            return
        for execution in tool_executions:
            self.artifact_writer.append_tool_trace(
                round_num=round_num,
                payload=self.tool_check_from_execution(execution),
            )

    def persist_terminal_decision(
        self,
        *,
        round_num: int,
        decision: AgentTerminalDecision,
        evaluator_step: int,
    ) -> None:
        """Persist the evaluator runtime terminal decision.

        Args:
            round_num: Evaluated round number.
            decision: Shared terminal decision for evaluator execution.
            evaluator_step: Last evaluator loop step involved in the decision.
        """

        if self.artifact_writer is None:
            return
        self.artifact_writer.append_runtime_event(
            round_num=round_num,
            payload={
                "event": "agentkit_evaluator_terminal_decision",
                "runtime_phase": "evaluator",
                "phase_step": evaluator_step,
                "step": evaluator_step,
                "terminal_decision": decision.to_json(),
            },
        )

    @classmethod
    def tool_check_from_execution(cls, execution: ToolExecutionRecord) -> EvaluationToolCheck:
        """Convert one captured tool execution into a public check record.

        Args:
            execution: Captured evaluator tool execution.

        Returns:
            Public evaluator tool check record.
        """

        return EvaluationToolCheck(
            name=execution.tool_name,
            status=EvaluationToolCheckStatus.FAILURE if execution.is_error else EvaluationToolCheckStatus.SUCCESS,
            summary=cls.summarize_tool_output(execution.llm_tool_output or execution.tool_output),
        )

    @classmethod
    def summarize_tool_output(cls, output: object) -> str:
        """Return a compact one-line-ish tool output summary.

        Args:
            output: Raw tool output.

        Returns:
            Prompt-safe summary string.
        """

        if output is None:
            return ""
        if isinstance(output, str):
            return output[:1000]
        return json.dumps(cls.strip_large_payloads(output), ensure_ascii=False)[:1000]

    @classmethod
    def redact_json_object(cls, payload: object) -> JsonObject:
        """Redact sensitive and bulky values from a JSON-like object.

        Args:
            payload: JSON-like payload to redact.

        Returns:
            Redacted JSON object, or an empty object when input is not a mapping.
        """

        if not isinstance(payload, dict):
            return {}
        return {str(key): cls.strip_large_payloads(value, key=str(key)) for key, value in payload.items()}

    @classmethod
    def redact_sensitive_json_object(cls, payload: object) -> JsonObject:
        """Redact secret-like keys while preserving image/tool payloads for replay.

        Args:
            payload: JSON-like object to redact.

        Returns:
            Redacted JSON object, or an empty object for non-mapping input.
        """

        if not isinstance(payload, dict):
            return {}
        return {str(key): cls.redact_sensitive_json_value(value, key=str(key)) for key, value in payload.items()}

    @classmethod
    def redact_sensitive_json_value(cls, value: object, *, key: str = "") -> object:
        """Recursively redact secret-like fields without compacting media payloads.

        Args:
            value: JSON-like value to redact.
            key: Optional containing key used for secret detection.

        Returns:
            Redacted value with image/tool payloads preserved.
        """

        lowered = key.lower()
        if any(
            fragment in lowered
            for fragment in ("authorization", "api_key", "apikey", "secret", "token", "safety_identifier")
        ):
            return "[redacted sensitive value]"
        if isinstance(value, dict):
            return {
                str(item_key): cls.redact_sensitive_json_value(item, key=str(item_key))
                for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [cls.redact_sensitive_json_value(item) for item in value]
        return value

    @classmethod
    def strip_large_payloads(cls, value: object, *, key: str = "") -> object:
        """Remove secrets and inline images from trace payloads recursively.

        Args:
            value: JSON-like value to redact.
            key: Optional containing key used for secret detection.

        Returns:
            Redacted value.
        """

        lowered = key.lower()
        if any(
            fragment in lowered
            for fragment in ("authorization", "api_key", "apikey", "secret", "token", "safety_identifier")
        ):
            return "[redacted sensitive value]"
        if isinstance(value, str):
            if value.startswith("data:image/"):
                return f"[redacted image data url; chars={len(value)}]"
            return value
        if isinstance(value, dict):
            return {
                str(item_key): cls.strip_large_payloads(item, key=str(item_key)) for item_key, item in value.items()
            }
        if isinstance(value, list):
            return [cls.strip_large_payloads(item) for item in value]
        return value


__all__ = ["EvaluatorTraceWriter"]
