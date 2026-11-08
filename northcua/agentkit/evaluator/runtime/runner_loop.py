"""Internal evaluator round loop used by ``AgentKitEvaluatorRunner``."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

from nexau.archs.tool.tool import Tool
from nexau.core.messages import Message, Role, TextBlock
from pydantic import BaseModel

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.agentkit.runtime import (
    AgentRoundRuntime,
    AgentRoundRuntimeSpec,
    AgentRuntimeCounterState,
    AgentRuntimePhase,
    AgentRuntimeStepContext,
    AgentRuntimeStepOutcome,
    AgentSessionRuntime,
    AgentStepRuntime,
)
from northcua.core.budget import AgentBudgetPolicy, AgentBudgetScope, AgentStepBudget
from northcua.core.enums import EvaluationToolCheckStatus
from northcua.core.models import ToolExecutionRecord
from northcua.core.terminal import AgentTerminalDecision, AgentTerminalReason

from ..protocol import EvaluationJudgeMetadata, EvaluationToolCheck, RoundEvaluationRecord, RoundEvaluationReport
from ..routing import EvaluatorClientToolDispatcher
from .contracts import EvaluatorPromptEnvelope, EvaluatorRunContext, EvaluatorRunResult
from .tool_calls import pending_tool_calls_from_meta
from .trace_context import default_evaluator_trace_context

if TYPE_CHECKING:
    from .runner import AgentKitEvaluatorRunner

logger = logging.getLogger(__name__)
_FINAL_REPORT_TOOL_NAME = "final_report"
_REPAIR_RESPONSE_MAX_CHARS = 12_000
_FINAL_REPORT_ERROR_REPAIR_MAX_ATTEMPTS = 2
_FINAL_REPORT_REPAIR_EVIDENCE_REF_LIMIT = 80


@dataclass(frozen=True, slots=True)
class _EvaluatorRuntimeStepRecord:
    """Runtime-local record for one evaluator model/tool step."""

    evaluator_step: int
    response_text: str


async def _run_evaluator_round(
    runner: "AgentKitEvaluatorRunner",
    *,
    context: EvaluatorRunContext,
    messages: list[Message],
    trace_context: JsonObject | None,
    externalize_tools: bool,
    client_tool_dispatcher: EvaluatorClientToolDispatcher | None,
) -> EvaluatorRunResult:
    if runner.model_step_executor is None:
        raise RuntimeError("AgentKitEvaluatorRunner.run_round requires a model_step_executor.")
    model_step_executor = runner.model_step_executor
    runner.persist_inputs(
        round_num=context.bundle.round_num, bundle=context.bundle, evidence_manifest=context.evidence_manifest
    )
    toolset = runner.build_toolset(context, client_tool_dispatcher=client_tool_dispatcher)
    base_messages = list(messages)
    current_messages = list(base_messages)
    response_text = ""
    terminal_decision: AgentTerminalDecision | None = None
    last_evaluator_step = 0
    terminal_decision_persisted = False
    finalizer_repair_mode = False
    final_report_error_repair_count = 0

    async def _execute_step(
        runtime_context: AgentRuntimeStepContext[object, object],
    ) -> AgentRuntimeStepOutcome[_EvaluatorRuntimeStepRecord]:
        nonlocal current_messages
        nonlocal finalizer_repair_mode
        nonlocal last_evaluator_step
        nonlocal response_text
        nonlocal terminal_decision
        nonlocal terminal_decision_persisted
        nonlocal final_report_error_repair_count
        evaluator_step = runtime_context.cursor.phase_step
        last_evaluator_step = evaluator_step
        step_started_at = time.perf_counter()
        active_tools = _finalizer_only_tools(toolset.tools) if finalizer_repair_mode else toolset.tools
        result = await model_step_executor.run_step(
            agent_kind=runner.spec.kind,
            session_id=context.bundle.session_id,
            step_index=evaluator_step,
            build_envelope=lambda provider_tools, tool_choice: EvaluatorPromptEnvelope(
                messages=current_messages,
                provider_tools=list(provider_tools),
                tool_choice="required" if finalizer_repair_mode else tool_choice,
            ),
            envelope_messages=lambda envelope: envelope.messages,
            context=default_evaluator_trace_context(spec=runner.spec, context=context, trace_context=trace_context),
            remote=context.remote,
            externalize_tools=externalize_tools,
            tool_choice_override="required" if finalizer_repair_mode else None,
            configure_agent=lambda agent_config, active_tools=active_tools: runner._configure_model_agent(
                agent_config, active_tools
            ),
        )
        response_text = result.response_text
        pending_tool_calls = pending_tool_calls_from_meta(result.meta)
        if not pending_tool_calls:
            if _should_force_finalizer_repair(runner=runner, evaluator_step=evaluator_step):
                current_messages = _append_full_history_finalizer_repair(
                    runner=runner,
                    messages=current_messages,
                    context=context,
                    trace_state=result.trace_state,
                    tool_executions=[],
                )
            else:
                current_messages = _append_missing_final_report_repair(
                    runner=runner,
                    base_messages=base_messages,
                    context=context,
                    response_text=response_text,
                    trace_state=result.trace_state,
                )
            finalizer_repair_mode = True
            runner.trace_writer.persist_step_trace(
                session_id=context.bundle.session_id,
                round_num=context.bundle.round_num,
                evaluator_step=evaluator_step,
                request=context.bundle.round_request,
                trace_state=result.trace_state,
                duration_ms=(time.perf_counter() - step_started_at) * 1000,
            )
            terminal_decision = AgentTerminalDecision.continue_(
                summary="The evaluator did not call final_report, so the runtime requested an explicit final_report retry.",
                final_answer=response_text,
                metadata={"missing_tool": "final_report", "repair": "explicit_final_report_retry"},
            )
            return _step_outcome(runtime_context, evaluator_step, response_text, terminal_decision)
        tool_executions = await runner.tool_call_executor.execute(
            pending_tool_calls=pending_tool_calls, trace_state=result.trace_state, tools=active_tools
        )
        runner.trace_writer.persist_tool_traces(round_num=context.bundle.round_num, tool_executions=tool_executions)
        runner.trace_writer.persist_step_trace(
            session_id=context.bundle.session_id,
            round_num=context.bundle.round_num,
            evaluator_step=evaluator_step,
            request=context.bundle.round_request,
            trace_state=result.trace_state,
            duration_ms=(time.perf_counter() - step_started_at) * 1000,
        )
        if final_report_error := _final_report_error_summary(tool_executions):
            if (
                final_report_error_repair_count < _FINAL_REPORT_ERROR_REPAIR_MAX_ATTEMPTS
                and evaluator_step < runner.spec.max_steps
            ):
                final_report_error_repair_count += 1
                if _should_force_finalizer_repair(runner=runner, evaluator_step=evaluator_step):
                    current_messages = _append_full_history_finalizer_repair(
                        runner=runner,
                        messages=current_messages,
                        context=context,
                        trace_state=result.trace_state,
                        tool_executions=tool_executions,
                        repair_reason=f"final_report_tool_error: {final_report_error}",
                    )
                else:
                    current_messages = _append_missing_final_report_repair(
                        runner=runner,
                        base_messages=base_messages,
                        context=context,
                        response_text=response_text,
                        trace_state=result.trace_state,
                        latest_tool_result_summary=_tool_result_summary(tool_executions),
                        repair_reason=f"final_report_tool_error: {final_report_error}",
                    )
                finalizer_repair_mode = True
                terminal_decision = AgentTerminalDecision.continue_(
                    summary=(
                        "The evaluator submitted an invalid final_report, so the runtime requested one "
                        "finalizer-only repair with valid evidence refs."
                    ),
                    final_answer=response_text,
                    metadata={
                        "missing_tool": "final_report",
                        "repair": "final_report_tool_error",
                        "repair_attempt": final_report_error_repair_count,
                    },
                )
                return _step_outcome(runtime_context, evaluator_step, response_text, terminal_decision)
            error_decision = _final_report_error_decision(final_report_error)
            terminal_decision = error_decision
            runner.trace_writer.persist_terminal_decision(
                round_num=context.bundle.round_num, decision=terminal_decision, evaluator_step=evaluator_step
            )
            terminal_decision_persisted = True
            return _step_outcome(runtime_context, evaluator_step, response_text, terminal_decision)
        if toolset.final_report_state.submitted:
            terminal_decision = AgentTerminalDecision.finish(
                final_answer=response_text, summary="The evaluator submitted a valid final_report."
            )
            runner.trace_writer.persist_terminal_decision(
                round_num=context.bundle.round_num, decision=terminal_decision, evaluator_step=evaluator_step
            )
            terminal_decision_persisted = True
            return _step_outcome(runtime_context, evaluator_step, response_text, terminal_decision)
        if _should_force_finalizer_repair(runner=runner, evaluator_step=evaluator_step):
            current_messages = _append_full_history_finalizer_repair(
                runner=runner,
                messages=current_messages,
                context=context,
                trace_state=result.trace_state,
                tool_executions=tool_executions,
            )
            finalizer_repair_mode = True
            terminal_decision = AgentTerminalDecision.continue_(
                summary=(
                    "The evaluator reached its evidence-gathering soft limit, so the runtime requested a "
                    "finalizer-only report."
                ),
                final_answer=response_text,
                metadata={
                    "missing_tool": "final_report",
                    "repair": "evidence_step_limit",
                    "force_finalizer_after_steps": runner.spec.force_finalizer_after_steps,
                },
            )
            return _step_outcome(runtime_context, evaluator_step, response_text, terminal_decision)
        current_messages = runner.prompt_history_replayer.append_tool_step_history(
            messages=current_messages, trace_state=result.trace_state, tool_executions=tool_executions
        )
        terminal_decision = AgentTerminalDecision.continue_(
            summary="The evaluator needs another model step before final_report.", final_answer=response_text
        )
        return _step_outcome(runtime_context, evaluator_step, response_text, terminal_decision)

    try:
        round_runtime = AgentRoundRuntime(
            session_runtime=AgentSessionRuntime(
                counters=AgentRuntimeCounterState(
                    session_id=context.bundle.session_id, current_round_num=max(0, context.bundle.round_num - 1)
                )
            ),
            step_runtime=cast(
                AgentStepRuntime[object, object, _EvaluatorRuntimeStepRecord],
                AgentStepRuntime(step_executor=_execute_step),
            ),
        )
        round_result = await round_runtime.run(
            AgentRoundRuntimeSpec(
                query=context.bundle.round_request,
                budget=AgentStepBudget(max_round_steps=runner.spec.max_steps),
                fallback_round_steps=runner.spec.max_steps,
                phase=AgentRuntimePhase.EVALUATOR,
            )
        )
        terminal_decision = round_result.terminal_decision
        if round_result.steps:
            last_evaluator_step = round_result.steps[-1].evaluator_step
    except Exception as exc:
        logger.exception(
            "AgentKit evaluator runtime failed",
            extra={
                "evaluator_kind": runner.spec.kind,
                "session_id": context.bundle.session_id,
                "round_num": context.bundle.round_num,
                "evaluator_step": max(1, last_evaluator_step),
            },
        )
        terminal_decision = AgentTerminalDecision.error(
            reason=AgentTerminalReason.RUNTIME_ERROR,
            final_answer=str(exc),
            summary="The evaluator encountered a runtime error.",
            retryable=True,
        )
        runner.trace_writer.persist_terminal_decision(
            round_num=context.bundle.round_num,
            decision=terminal_decision,
            evaluator_step=max(1, last_evaluator_step),
        )
        raise
    if toolset.final_report_state.report is None:
        _persist_missing_final_report_decision(
            runner=runner,
            context=context,
            terminal_decision=terminal_decision,
            terminal_decision_persisted=terminal_decision_persisted,
            last_evaluator_step=last_evaluator_step,
            tool_check=toolset.final_report_state.tool_check,
        )
        raise RuntimeError("Evaluator model completed without calling final_report.")
    report = _coerce_round_report(toolset.final_report_state.report)
    tool_checks = [toolset.final_report_state.tool_check] if toolset.final_report_state.tool_check else []
    record = RoundEvaluationRecord.from_report(
        session_id=context.bundle.session_id,
        round_num=context.bundle.round_num,
        round_request=context.bundle.round_request,
        report=report,
        evidence_manifest=context.evidence_manifest,
        tool_checks=tool_checks,
        judge_metadata=EvaluationJudgeMetadata(),
    )
    runner.persist_final_report(round_num=context.bundle.round_num, report=report)
    terminal_decision = terminal_decision or AgentTerminalDecision.finish(
        final_answer=response_text, summary="The evaluator submitted a valid final_report."
    )
    return EvaluatorRunResult(
        record=record, response_text=response_text, toolset=toolset, terminal_decision=terminal_decision
    )


def _append_full_history_finalizer_repair(
    *,
    runner: "AgentKitEvaluatorRunner",
    messages: list[Message],
    context: EvaluatorRunContext,
    trace_state: TraceCaptureState,
    tool_executions: list[ToolExecutionRecord],
    repair_reason: str = "evidence_step_limit",
) -> list[Message]:
    """Keep the complete evidence history when the probing budget is exhausted."""

    history = runner.prompt_history_replayer.append_tool_step_history(
        messages=messages,
        trace_state=trace_state,
        tool_executions=tool_executions,
    )
    history.append(
        Message(
            role=Role.USER,
            content=[
                TextBlock(
                    text=(
                        "Evidence-gathering step limit reached. The complete evaluator conversation and "
                        "model-visible tool history above has been retained without repair-summary compaction. "
                        "The next turn has "
                        "only the final_report tool. Submit one complete typed final_report now using the "
                        "evidence already collected. Do not request or repeat any evidence read, do not answer "
                        "with plain text, and do not stop after reasoning. If the retained evidence is "
                        "insufficient, submit final_report with needs_more_evidence=true.\n\n"
                        f"Evaluated task: {context.bundle.round_request}\n"
                        f"Repair reason: {repair_reason}"
                    )
                )
            ],
        )
    )
    return history


def _append_missing_final_report_repair(
    *,
    runner: "AgentKitEvaluatorRunner",
    base_messages: list[Message],
    context: EvaluatorRunContext,
    response_text: str,
    trace_state: TraceCaptureState,
    latest_tool_result_summary: str = "",
    repair_reason: str = "missing_final_report",
) -> list[Message]:
    history = _base_system_messages(base_messages)
    response_summary = _repair_response_summary(response_text=response_text, trace_state=trace_state)
    if response_summary:
        history.append(
            Message(
                role=Role.ASSISTANT,
                content=[
                    TextBlock(
                        text=(
                            "Previous evaluator response before the required final_report tool call:\n"
                            f"{response_summary}"
                        )
                    )
                ],
            )
        )
    if latest_tool_result_summary:
        history.append(
            Message(
                role=Role.USER,
                content=[
                    TextBlock(
                        text=(
                            "Latest evaluator tool-result summary before finalizer repair:\n"
                            f"{latest_tool_result_summary}"
                        )
                    )
                ],
            )
        )
    history.append(
        Message(
            role=Role.USER,
            content=[
                TextBlock(
                    text=(
                        "Finalizer repair context.\n"
                        f"Evaluated task: {context.bundle.round_request}\n\n"
                        f"Repair reason: {repair_reason}.\n"
                        "The evaluator has not submitted the required final_report tool. The runtime "
                        "has switched this repair turn to the finalizer-only tool surface to avoid replaying bulky "
                        "artifact history. Convert your previous conclusion into one complete typed final_report "
                        "now. Do not call artifact tools, do not answer with plain text only, and do not stop after "
                        "reasoning. If your previous conclusion said evidence was insufficient, submit final_report "
                        "with needs_more_evidence=true."
                        "\n\nUse only evidence_ref ids from this valid manifest. Do not invent refs like "
                        "E_final_dom or E_trace. If a field does not need a specific evidence citation, use [].\n"
                        f"{_evidence_ref_summary(context.evidence_manifest)}"
                    )
                )
            ],
        )
    )
    return history


def _base_system_messages(messages: list[Message]) -> list[Message]:
    """Return the evaluator system preamble without bulky user/tool history."""

    return [message for message in messages if message.role == Role.SYSTEM]


def _repair_response_summary(*, response_text: str, trace_state: TraceCaptureState) -> str:
    """Return bounded evaluator conclusion text for a finalizer-only repair turn."""

    parts = [response_text.strip(), trace_state.assistant_text.strip(), trace_state.reasoning.strip()]
    seen: set[str] = set()
    selected: list[str] = []
    for part in parts:
        if not part or part in seen:
            continue
        seen.add(part)
        selected.append(part)
    summary = "\n\n".join(selected).strip()
    if len(summary) <= _REPAIR_RESPONSE_MAX_CHARS:
        return summary
    omitted = len(summary) - _REPAIR_RESPONSE_MAX_CHARS
    return f"{summary[:_REPAIR_RESPONSE_MAX_CHARS].rstrip()}\n\n[truncated {omitted} chars from repair summary]"


def _finalizer_only_tools(tools: tuple[Tool, ...]) -> tuple[Tool, ...]:
    """Return only the final_report tool for compact repair turns."""

    finalizers = tuple(tool for tool in tools if getattr(tool, "name", "") == _FINAL_REPORT_TOOL_NAME)
    return finalizers or tools


def _should_force_finalizer_repair(*, runner: "AgentKitEvaluatorRunner", evaluator_step: int) -> bool:
    """Return whether an evaluator should stop probing and submit a final report."""

    limit = runner.spec.force_finalizer_after_steps
    return limit is not None and evaluator_step >= limit


def _tool_result_summary(tool_executions: list[ToolExecutionRecord]) -> str:
    """Return a compact, path-free summary of latest evaluator tool results."""

    lines: list[str] = []
    for execution in tool_executions:
        status = "error" if execution.is_error else "ok"
        summary = _compact_tool_output_summary(execution.tool_output)
        line = f"- {execution.tool_name} [{status}]"
        if summary:
            line += f": {summary}"
        lines.append(line)
    return "\n".join(lines)


def _evidence_ref_summary(manifest: object) -> str:
    refs = list(getattr(manifest, "refs", []) or [])
    if not refs:
        return "Available evidence refs: none. Use [] for all evidence_refs fields."
    lines = ["Available evidence refs:"]
    for ref in refs[:_FINAL_REPORT_REPAIR_EVIDENCE_REF_LIMIT]:
        kind = getattr(getattr(ref, "kind", ""), "value", getattr(ref, "kind", ""))
        label = _truncate_inline(str(getattr(ref, "label", "") or ""), 120)
        summary = _truncate_inline(str(getattr(ref, "summary", "") or ""), 180)
        session_step = getattr(ref, "session_step", 0)
        reason = getattr(ref, "selection_reason", "") or ""
        parts = [str(getattr(ref, "id", "")), str(kind)]
        if session_step:
            parts.append(f"step={session_step}")
        if reason:
            parts.append(f"reason={_truncate_inline(str(reason), 80)}")
        if label:
            parts.append(f"label={label}")
        if summary:
            parts.append(f"summary={summary}")
        lines.append("- " + "; ".join(parts))
    if len(refs) > _FINAL_REPORT_REPAIR_EVIDENCE_REF_LIMIT:
        lines.append(f"- ... {len(refs) - _FINAL_REPORT_REPAIR_EVIDENCE_REF_LIMIT} more refs omitted")
    return "\n".join(lines)


def _compact_tool_output_summary(value: object) -> str:
    if isinstance(value, dict):
        for key in ("summary", "text", "error", "message", "status"):
            item = value.get(key)
            if isinstance(item, str) and item.strip():
                return _truncate_inline(item.strip(), 500)
        keys = ", ".join(sorted(str(key) for key in value.keys())[:8])
        return f"object keys: {keys}" if keys else ""
    if isinstance(value, str):
        return _truncate_inline(value.strip(), 500)
    if value is None:
        return ""
    return _truncate_inline(str(value), 500)


def _truncate_inline(value: str, max_chars: int) -> str:
    value = " ".join(value.split())
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()}...[truncated {len(value) - max_chars} chars]"


def _step_outcome(
    runtime_context: AgentRuntimeStepContext[object, object],
    evaluator_step: int,
    response_text: str,
    terminal_decision: AgentTerminalDecision,
) -> AgentRuntimeStepOutcome[_EvaluatorRuntimeStepRecord]:
    return AgentRuntimeStepOutcome(
        cursor=runtime_context.cursor,
        result=_EvaluatorRuntimeStepRecord(evaluator_step=evaluator_step, response_text=response_text),
        terminal_decision=terminal_decision,
        summary=terminal_decision.summary,
    )


def _final_report_error_summary(tool_executions: list[ToolExecutionRecord]) -> str:
    final_report_errors = [
        execution
        for execution in tool_executions
        if execution.tool_name == _FINAL_REPORT_TOOL_NAME and execution.is_error
    ]
    if not final_report_errors:
        return ""
    first_error = final_report_errors[0].tool_output
    return (
        str(first_error.get("error") or first_error.get("summary") or first_error)
        if isinstance(first_error, dict)
        else str(first_error)
    )


def _final_report_error_decision(error_text: str) -> AgentTerminalDecision | None:
    if not error_text:
        return None
    return AgentTerminalDecision.fail(
        reason=AgentTerminalReason.TOOL_ERROR,
        final_answer=error_text,
        summary="The evaluator final_report tool rejected the submitted payload.",
        retryable=True,
    )


def _persist_missing_final_report_decision(
    *,
    runner: "AgentKitEvaluatorRunner",
    context: EvaluatorRunContext,
    terminal_decision: AgentTerminalDecision | None,
    terminal_decision_persisted: bool,
    last_evaluator_step: int,
    tool_check: EvaluationToolCheck | None,
) -> None:
    if terminal_decision_persisted:
        return
    if tool_check is not None and tool_check.status == EvaluationToolCheckStatus.FAILURE:
        terminal_decision = AgentTerminalDecision.fail(
            reason=AgentTerminalReason.TOOL_ERROR,
            final_answer=tool_check.summary,
            summary="The evaluator final_report tool rejected the submitted payload.",
            retryable=True,
        )
    elif terminal_decision is None or terminal_decision.reason == AgentTerminalReason.MAX_STEPS:
        terminal_decision = AgentBudgetPolicy.max_step_decision(
            scope=AgentBudgetScope.ROUND,
            limit=runner.spec.max_steps,
            current=max(1, last_evaluator_step),
            final_answer="Evaluator reached max_steps without calling final_report.",
            summary="The evaluator reached its round step limit.",
        )
    runner.trace_writer.persist_terminal_decision(
        round_num=context.bundle.round_num,
        decision=terminal_decision,
        evaluator_step=max(1, last_evaluator_step),
    )


def _coerce_round_report(report: BaseModel) -> RoundEvaluationReport:
    if isinstance(report, RoundEvaluationReport):
        return report
    return RoundEvaluationReport.model_validate(report.model_dump(mode="json"))
