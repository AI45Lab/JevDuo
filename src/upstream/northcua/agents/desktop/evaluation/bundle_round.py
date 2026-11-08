"""Round-level evaluator bundle projection helpers."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from northcua.agentkernel.json_types import JsonObject
from northcua.core.accessibility import AccessibilityStepRecord
from northcua.core.enums import EvaluationEvidenceKind
from northcua.core.models import StepLogRecord

from .protocol import EvaluationEvidenceRef, RoundEvaluationBundle, RoundEvaluationStepRef

if TYPE_CHECKING:
    from .bundle import BundleBuilder


def build_round_bundle(
    builder: BundleBuilder, *, round_num: int, round_request: str, step_logs: list[StepLogRecord]
) -> RoundEvaluationBundle:
    """Build one round evaluation bundle from persisted step logs.

    Args:
        builder: Bundle builder that owns artifact store access and policy.
        round_num: Round number being evaluated.
        round_request: User request associated with the round.
        step_logs: Persisted session step records accumulated so far.

    Returns:
        Canonical evaluator bundle containing step, image, a11y, runtime, and
        prior-round evidence references.
    """

    round_steps = [step for step in step_logs if step.round_num == round_num]
    session_request = step_logs[0].request if step_logs else round_request
    evidence_counter = 0

    def next_evidence_id() -> str:
        """Return the next stable bundle-local evidence id.

        Returns:
            Evidence id in deterministic encounter order.
        """

        nonlocal evidence_counter
        evidence_counter += 1
        return f"E{evidence_counter:03d}"

    screenshot_refs: list[EvaluationEvidenceRef] = []
    a11y_candidates: list[tuple[StepLogRecord, AccessibilityStepRecord]] = []
    runtime_event_refs = builder._build_runtime_event_refs(round_steps, next_evidence_id)
    step_refs: list[RoundEvaluationStepRef] = []
    step_summaries: list[str] = []
    assistant_texts: list[str] = []
    tool_calls: list[JsonObject] = []
    tool_results: list[JsonObject] = []
    seen_image_paths: set[str] = set()
    has_initial_screenshot = False
    for step in round_steps:
        step_refs.append(_step_ref(step))
        step_summaries.append(builder._summarize_step(step))
        if step.assistant_text.strip():
            assistant_texts.append(step.assistant_text.strip())
        tool_calls.extend([call.model_dump(mode="json") for call in step.assistant_tool_calls])
        tool_results.extend(_tool_result_payloads(builder, step))
        has_initial_screenshot = _append_step_images(
            builder,
            step=step,
            screenshot_refs=screenshot_refs,
            seen_image_paths=seen_image_paths,
            has_initial_screenshot=has_initial_screenshot,
            next_evidence_id=next_evidence_id,
        )
        if builder.config.include_a11y and step.accessibility is not None:
            a11y_candidates.append((step, step.accessibility))
    a11y_refs = builder._build_a11y_refs(a11y_candidates, next_evidence_id)
    return RoundEvaluationBundle(
        session_id=step_logs[0].session_id if step_logs else "",
        round_num=round_num,
        round_request=round_request,
        session_request=session_request,
        step_refs=step_refs,
        step_summaries=step_summaries,
        assistant_texts=assistant_texts,
        tool_calls=tool_calls,
        tool_results=tool_results,
        screenshot_refs=screenshot_refs,
        a11y_refs=a11y_refs,
        runtime_event_refs=runtime_event_refs if builder.config.include_runtime_events else [],
        prior_round_summaries=builder._load_prior_round_summaries(round_num),
    )


def _step_ref(step: StepLogRecord) -> RoundEvaluationStepRef:
    return RoundEvaluationStepRef(
        session_step=step.session_step,
        round_step=step.round_step,
        status=step.status.value,
        request=step.request,
        result=step.result,
        assistant_text=step.assistant_text,
        clean_screenshot_path=step.clean_screenshot_path,
    )


def _tool_result_payloads(builder: BundleBuilder, step: StepLogRecord) -> list[JsonObject]:
    return [
        {
            **builder._sanitize_tool_result_payload(artifact.model_dump(mode="json")),
            "session_step": step.session_step,
            "round_step": step.round_step,
        }
        for artifact in step.tool_results
    ]


def _append_step_images(
    builder: BundleBuilder,
    *,
    step: StepLogRecord,
    screenshot_refs: list[EvaluationEvidenceRef],
    seen_image_paths: set[str],
    has_initial_screenshot: bool,
    next_evidence_id: Callable[[], str],
) -> bool:
    is_terminal_verification = builder._is_terminal_verification_step(step)
    has_initial_screenshot = _append_preverification_image(
        step=step,
        screenshot_refs=screenshot_refs,
        seen_image_paths=seen_image_paths,
        has_initial_screenshot=has_initial_screenshot,
        is_terminal_verification=is_terminal_verification,
        next_evidence_id=next_evidence_id,
    )
    has_initial_screenshot = _append_clean_screenshot(
        step=step,
        screenshot_refs=screenshot_refs,
        seen_image_paths=seen_image_paths,
        has_initial_screenshot=has_initial_screenshot,
        is_terminal_verification=is_terminal_verification,
        next_evidence_id=next_evidence_id,
    )
    _append_tool_images(
        step=step,
        screenshot_refs=screenshot_refs,
        seen_image_paths=seen_image_paths,
        next_evidence_id=next_evidence_id,
    )
    return has_initial_screenshot


def _append_preverification_image(
    *,
    step: StepLogRecord,
    screenshot_refs: list[EvaluationEvidenceRef],
    seen_image_paths: set[str],
    has_initial_screenshot: bool,
    is_terminal_verification: bool,
    next_evidence_id: Callable[[], str],
) -> bool:
    previous_clean = step.observation_metadata.get("round_end_verification_previous_clean_screenshot_path")
    if (
        not is_terminal_verification
        or not isinstance(previous_clean, str)
        or not previous_clean
        or previous_clean in seen_image_paths
        or has_initial_screenshot
    ):
        return has_initial_screenshot
    screenshot_refs.append(
        EvaluationEvidenceRef(
            id=next_evidence_id(),
            kind=EvaluationEvidenceKind.IMAGE,
            label=f"Step {step.session_step} pre-verification screenshot",
            summary=(
                "Earliest available clean screenshot for the round, captured before terminal "
                "verification replaced the current screenshot."
            ),
            path=previous_clean,
            session_step=step.session_step,
            round_step=step.round_step,
            metadata={"variant": "initial_state", "initial_state": True},
        )
    )
    seen_image_paths.add(previous_clean)
    return True


def _append_clean_screenshot(
    *,
    step: StepLogRecord,
    screenshot_refs: list[EvaluationEvidenceRef],
    seen_image_paths: set[str],
    has_initial_screenshot: bool,
    is_terminal_verification: bool,
    next_evidence_id: Callable[[], str],
) -> bool:
    if not step.clean_screenshot_path or step.clean_screenshot_path in seen_image_paths:
        return has_initial_screenshot
    screenshot_metadata: JsonObject = {}
    if not has_initial_screenshot and not is_terminal_verification:
        screenshot_metadata.update({"variant": "initial_state", "initial_state": True})
        has_initial_screenshot = True
    if is_terminal_verification:
        screenshot_metadata.update(_terminal_screenshot_metadata(step))
    label = f"Step {step.session_step} current screenshot"
    summary = f"Current clean screenshot captured at step {step.session_step}."
    if screenshot_metadata.get("initial_state") and screenshot_metadata.get("variant") != "terminal_verification":
        label = f"Step {step.session_step} initial screenshot"
        summary = (
            "Earliest available clean screenshot for the round, captured after case setup "
            f"and before or at step {step.session_step}."
        )
    screenshot_refs.append(
        EvaluationEvidenceRef(
            id=next_evidence_id(),
            kind=EvaluationEvidenceKind.IMAGE,
            label=label,
            summary=summary,
            path=step.clean_screenshot_path,
            session_step=step.session_step,
            round_step=step.round_step,
            metadata=screenshot_metadata,
        )
    )
    seen_image_paths.add(step.clean_screenshot_path)
    return has_initial_screenshot


def _terminal_screenshot_metadata(step: StepLogRecord) -> JsonObject:
    metadata: JsonObject = {
        "variant": "terminal_verification",
        "pinned": True,
        "pinned_reason": "desktop_terminal_verification",
        "source": step.observation_metadata.get("round_end_verification_source") or "",
    }
    if coordinate_space := step.observation_metadata.get("round_end_verification_coordinate_space"):
        metadata["coordinate_space"] = coordinate_space
    return metadata


def _append_tool_images(
    *,
    step: StepLogRecord,
    screenshot_refs: list[EvaluationEvidenceRef],
    seen_image_paths: set[str],
    next_evidence_id: Callable[[], str],
) -> None:
    for tool_index, artifact in enumerate(step.tool_results, start=1):
        selected_path = artifact.overlay_image_path or artifact.image_path or artifact.raw_image_path
        selected_summary = artifact.content.strip() or f"Tool result from {artifact.name}"
        if selected_path and selected_path not in seen_image_paths:
            selected_variant = (
                "overlay"
                if selected_path == artifact.overlay_image_path
                else artifact.selected_image_kind.value or "selected"
            )
            screenshot_refs.append(
                EvaluationEvidenceRef(
                    id=next_evidence_id(),
                    kind=EvaluationEvidenceKind.IMAGE,
                    label=f"Step {step.session_step} tool {tool_index} selected image",
                    summary=selected_summary,
                    path=selected_path,
                    session_step=step.session_step,
                    round_step=step.round_step,
                    metadata={
                        "tool_name": artifact.name,
                        "selected_image_kind": selected_variant,
                        "variant": selected_variant,
                    },
                )
            )
            seen_image_paths.add(selected_path)
        raw_path = artifact.raw_image_path
        if raw_path and raw_path not in seen_image_paths:
            screenshot_refs.append(
                EvaluationEvidenceRef(
                    id=next_evidence_id(),
                    kind=EvaluationEvidenceKind.IMAGE,
                    label=f"Step {step.session_step} tool {tool_index} raw image",
                    summary=f"Raw post-tool screenshot from {artifact.name}.",
                    path=raw_path,
                    session_step=step.session_step,
                    round_step=step.round_step,
                    metadata={"tool_name": artifact.name, "variant": "raw"},
                )
            )
            seen_image_paths.add(raw_path)
