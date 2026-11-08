"""Trace context helpers for evaluator runtime runs."""

from __future__ import annotations

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.evaluator.spec import EvaluatorSpec

from .contracts import EvaluatorRunContext


def default_evaluator_trace_context(
    *,
    spec: EvaluatorSpec,
    context: EvaluatorRunContext,
    trace_context: JsonObject | None,
) -> JsonObject:
    """Build the standard trace metadata for one evaluator model step.

    Args:
        spec: Evaluator spec being executed.
        context: Runtime context for the evaluated round.
        trace_context: Optional caller-provided trace metadata.

    Returns:
        JSON object passed into the shared model step executor trace context.
    """

    payload: JsonObject = {
        "agent_kind": spec.kind,
        "evaluator_kind": spec.kind,
        "northcua_session_id": context.bundle.session_id,
        "round_num": context.bundle.round_num,
        "input_scope": spec.input_scope.value,
        "context_artifact_root": str(
            context.artifact_root / "evaluations" / spec.kind / f"round_{context.bundle.round_num:04d}"
        ),
    }
    payload.update(trace_context or {})
    return payload


__all__ = ["default_evaluator_trace_context"]
