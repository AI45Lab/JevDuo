"""Round evaluation bundle construction from persisted NorthCUA artifacts."""

from __future__ import annotations

import json
from collections.abc import Callable

from northcua.agentkernel.json_types import JsonObject
from northcua.core.accessibility import AccessibilityStepRecord
from northcua.core.config import EvaluationBundleConfig
from northcua.core.enums import AccessibilityCaptureStatus, EvaluationEvidenceKind, ObservationArtifactKind
from northcua.core.models import StepLogRecord
from northcua.runtime.artifacts.store import NorthCUAArtifactStore

from .bundle_round import build_round_bundle
from .protocol import (
    EvaluationEvidenceRef,
    PriorRoundEvaluationSummary,
    RoundEvaluationBundle,
    RoundEvaluationRecord,
)


class BundleBuilder:
    """Construct typed round bundles from NorthCUA step logs and artifacts."""

    _IMAGE_DATA_KEYS = frozenset({"raw_image_url", "overlay_image_url", "image_url"})
    _EVALUATOR_RUNTIME_EVENTS = frozenset(
        {
            "round_snapshot_ready",
            "round_stop_decision_made",
            "tool_result_artifact_built",
            "round_end_verification_capture_completed",
            "round_terminal_status_reached",
        }
    )

    def __init__(self, *, artifact_store: NorthCUAArtifactStore, config: EvaluationBundleConfig) -> None:
        """Initialize the bundle builder.

        Args:
            artifact_store: Store used to read persisted runtime artifacts.
            config: Bundle-selection policy controlling optional evidence.
        """
        self.artifact_store = artifact_store
        self.config = config

    def build(self, *, round_num: int, round_request: str, step_logs: list[StepLogRecord]) -> RoundEvaluationBundle:
        """Build one round bundle from all persisted state available so far.

        Args:
            round_num: Round number being evaluated.
            round_request: User request associated with the round.
            step_logs: Persisted session step records accumulated so far.

        Returns:
            A canonical evaluator bundle containing trace, image, accessibility,
            runtime-event, and prior-round evidence references.
        """
        return build_round_bundle(self, round_num=round_num, round_request=round_request, step_logs=step_logs)

    def _load_prior_round_summaries(self, current_round_num: int) -> list[PriorRoundEvaluationSummary]:
        """Load bounded summaries for rounds before the current one.

        Args:
            current_round_num: Round currently being evaluated.

        Returns:
            Prior summaries ordered by record order and capped by bundle config.
        """
        path = self.artifact_store.round_evaluation_log_path
        if not path.exists():
            return []
        records: list[PriorRoundEvaluationSummary] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = RoundEvaluationRecord.model_validate_json(line)
            if record.round_num >= current_round_num:
                continue
            records.append(
                PriorRoundEvaluationSummary(
                    round_num=record.round_num,
                    overall_verdict=record.overall_verdict,
                    completion_rating=record.completion_rating,
                    reason=record.reason,
                )
            )
        if self.config.include_prior_round_summaries <= 0:
            return []
        return records[-self.config.include_prior_round_summaries :]

    def _build_a11y_refs(
        self, candidates: list[tuple[StepLogRecord, AccessibilityStepRecord]], next_evidence_id: Callable[[], str]
    ) -> list[EvaluationEvidenceRef]:
        """Build accessibility evidence refs, collapsing repeated unavailable captures."""
        refs: list[EvaluationEvidenceRef] = []
        grouped_unavailable: dict[tuple[str, str, str, str], list[tuple[StepLogRecord, AccessibilityStepRecord]]] = {}
        for step, accessibility in candidates:
            if accessibility.status == AccessibilityCaptureStatus.SUCCESS:
                refs.append(self._a11y_ref(step, accessibility, next_evidence_id))
                if diff_ref := self._a11y_diff_ref(step, accessibility, next_evidence_id):
                    refs.append(diff_ref)
                continue
            key = (
                accessibility.status.value,
                str(accessibility.source.value if accessibility.source is not None else ""),
                accessibility.scope.value,
                accessibility.error,
            )
            grouped_unavailable.setdefault(key, []).append((step, accessibility))
        for group in grouped_unavailable.values():
            if len(group) == 1:
                step, accessibility = group[0]
                refs.append(self._a11y_ref(step, accessibility, next_evidence_id))
                continue
            first_step, first_accessibility = group[0]
            steps = [step.session_step for step, _accessibility in group]
            refs.append(
                EvaluationEvidenceRef(
                    id=next_evidence_id(),
                    kind=EvaluationEvidenceKind.A11Y,
                    label=f"Accessibility {first_accessibility.status.value} summary",
                    summary=f"Accessibility capture was {first_accessibility.status.value} for {len(group)} steps ({steps[0]}-{steps[-1]}): {first_accessibility.error or 'no details'}",
                    session_step=first_step.session_step,
                    round_step=first_step.round_step,
                    metadata={
                        **first_accessibility.model_dump(mode="json"),
                        "aggregated": True,
                        "affected_steps": steps,
                        "original_ref_count": len(group),
                    },
                )
            )
        return refs

    def _a11y_ref(
        self, step: StepLogRecord, accessibility: AccessibilityStepRecord, next_evidence_id: Callable[[], str]
    ) -> EvaluationEvidenceRef:
        """Build one step-scoped accessibility evidence ref."""
        a11y_summary = f"Accessibility snapshot with {accessibility.node_count} nodes; focused={accessibility.focused_role or 'none'}:{accessibility.focused_name or 'none'}."
        return EvaluationEvidenceRef(
            id=next_evidence_id(),
            kind=EvaluationEvidenceKind.A11Y,
            label=f"Step {step.session_step} accessibility snapshot",
            summary=a11y_summary,
            path=accessibility.artifact_path,
            session_step=step.session_step,
            round_step=step.round_step,
            metadata=accessibility.model_dump(mode="json"),
        )

    def _a11y_diff_ref(
        self, step: StepLogRecord, accessibility: AccessibilityStepRecord, next_evidence_id: Callable[[], str]
    ) -> EvaluationEvidenceRef | None:
        """Build one step-scoped accessibility diff evidence ref when present."""
        if accessibility.diff is None:
            return None
        diff_artifact = next(
            (
                artifact
                for artifact in step.observation_artifacts
                if artifact.kind == ObservationArtifactKind.ACCESSIBILITY_DIFF and artifact.path
            ),
            None,
        )
        if diff_artifact is None:
            return None
        diff_payload = accessibility.diff.model_dump(mode="json")
        changed_total = (
            int(diff_payload.get("added_nodes") or 0)
            + int(diff_payload.get("removed_nodes") or 0)
            + int(diff_payload.get("changed_nodes") or 0)
        )
        return EvaluationEvidenceRef(
            id=next_evidence_id(),
            kind=EvaluationEvidenceKind.A11Y,
            label=f"Step {step.session_step} accessibility diff",
            summary=(
                f"Accessibility diff changed {changed_total} nodes; "
                f"focus_changed={bool(diff_payload.get('focus_changed'))}."
            ),
            path=diff_artifact.path,
            session_step=step.session_step,
            round_step=step.round_step,
            metadata={
                "variant": "accessibility_diff",
                "artifact_kind": diff_artifact.kind.value,
                **diff_payload,
            },
        )

    def _build_runtime_event_refs(
        self, round_steps: list[StepLogRecord], next_evidence_id: Callable[[], str]
    ) -> list[EvaluationEvidenceRef]:
        """Build evidence references for runtime events tied to round steps.

        Args:
            round_steps: Step records for the round under evaluation.
            next_evidence_id: Callable that returns the next bundle-local
                evidence id.

        Returns:
            Runtime event evidence references matching the round's session steps.
        """
        path = self.artifact_store.runtime_log_path
        if not path.exists() or not round_steps:
            return []
        session_steps = {step.session_step for step in round_steps}
        refs: list[EvaluationEvidenceRef] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            payload = json.loads(line)
            extra = payload.get("extra") or {}
            session_step = int(extra.get("session_step") or 0)
            if session_step not in session_steps:
                continue
            event_name = extra.get("event") or payload.get("message") or "runtime_event"
            if event_name not in self._EVALUATOR_RUNTIME_EVENTS:
                continue
            refs.append(
                EvaluationEvidenceRef(
                    id=next_evidence_id(),
                    kind=EvaluationEvidenceKind.RUNTIME,
                    label=f"Runtime event {event_name}",
                    summary=str(payload.get("message") or event_name),
                    session_step=session_step,
                    round_step=int(extra.get("round_step") or 0),
                    metadata={"event": event_name, "extra": extra},
                )
            )
        return refs

    @staticmethod
    def _summarize_step(step: StepLogRecord) -> str:
        """Create a compact human-readable step summary.

        Args:
            step: Persisted step record to summarize.

        Returns:
            One-line summary for evaluator prompts.
        """
        assistant = step.assistant_text.strip() or "Assistant emitted no user-visible text."
        result = step.result.strip() or "No terminal result text."
        return f"Step {step.session_step} status={step.status.value}. Assistant: {assistant} Result: {result}"

    @staticmethod
    def _is_terminal_verification_step(step: StepLogRecord) -> bool:
        """Return whether the step screenshot is a round-end verification image."""
        metadata = step.observation_metadata
        if metadata.get("round_end_verification_status") == "success":
            return True
        screenshot_path = step.clean_screenshot_path.lower()
        return screenshot_path.endswith("_verify.png") or "verification" in screenshot_path

    @classmethod
    def _sanitize_tool_result_payload(cls, payload: JsonObject) -> JsonObject:
        """Strip prompt-hostile image data URLs from persisted tool result IR."""
        return {key: cls._sanitize_value(value, key=key) for key, value in payload.items()}

    @classmethod
    def _sanitize_value(cls, value: object, *, key: str = "") -> object:
        if isinstance(value, str) and (key in cls._IMAGE_DATA_KEYS or value.startswith("data:image/")):
            if value.startswith("data:image/"):
                return f"[image data omitted; chars={len(value)}]"
            return value
        if isinstance(value, dict):
            return {str(item_key): cls._sanitize_value(item, key=str(item_key)) for item_key, item in value.items()}
        if isinstance(value, list):
            return [cls._sanitize_value(item) for item in value]
        return value
