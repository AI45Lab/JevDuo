"""Web result to AgentKit round evaluation bundle adapter."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from northcua.agentkit.evaluator.protocol import (
    EvaluationEvidenceRef,
    RoundEvaluationBundle,
    RoundEvaluationStepRef,
)
from northcua.core.enums import EvaluationEvidenceKind
from northcua.core.json_types import JsonObject

from ...domain.evaluation_artifacts import WebTerminalVerificationStatus, WebToolResultArtifact
from ...domain.results import WebAgentResult, WebStepRecord


@dataclass(slots=True)
class WebRoundEvaluationBundleAdapter:
    """Project a Web run into the shared evaluator bundle contract."""

    result: WebAgentResult
    round_num: int = 1

    def build_bundle(self) -> RoundEvaluationBundle:
        """Return an AgentKit bundle with Web screenshots, DOM, and tool evidence."""

        steps = [step for step in self.result.steps if step.round_num == self.round_num]
        evidence_builder = _EvidenceIdBuilder()
        screenshot_refs: list[EvaluationEvidenceRef] = []
        dom_refs: list[EvaluationEvidenceRef] = []
        tool_results: list[JsonObject] = []
        first_session_step = min((step.session_step for step in steps), default=0)
        last_session_step = max((step.session_step for step in steps), default=0)
        image_state = _EvidencePathState()
        dom_state = _EvidencePathState()

        for step in steps:
            screenshot_refs.extend(
                self._step_screenshot_refs(
                    step,
                    evidence_builder,
                    state=image_state,
                )
            )
            dom_refs.extend(self._step_dom_refs(step, evidence_builder, state=dom_state))
            tool_results.extend(self._tool_results(step, evidence_builder, screenshot_refs, image_state))

        runtime_event_refs = self._runtime_event_refs(steps, evidence_builder)
        return RoundEvaluationBundle(
            session_id=self.result.session_id,
            round_num=self.round_num,
            round_request=self.result.user_query,
            session_request=self.result.user_query,
            step_refs=[self._step_ref(step) for step in steps],
            step_summaries=[self._step_summary(step) for step in steps],
            assistant_texts=[step.assistant_text for step in steps if step.assistant_text],
            tool_calls=[call.model_dump(mode="json") for step in steps for call in step.assistant_tool_calls],
            tool_results=tool_results,
            screenshot_refs=screenshot_refs,
            dom_refs=dom_refs,
            runtime_event_refs=runtime_event_refs,
        )

    def _step_ref(self, step: WebStepRecord) -> RoundEvaluationStepRef:
        return RoundEvaluationStepRef(
            session_step=step.session_step,
            round_step=step.step_index,
            status=step.status.value,
            request=self.result.user_query,
            result=step.error_text or step.tool_summary or self.result.final_answer,
            assistant_text=step.assistant_text,
            clean_screenshot_path=step.screenshot_path,
        )

    def _step_summary(self, step: WebStepRecord) -> str:
        page = step.observation_artifact
        title = page.page_title if page is not None else ""
        url = page.page_url if page is not None else step.url
        summary = step.tool_summary or step.error_text or "no tool"
        return f"Step {step.session_step}: {summary}; page={title or '(untitled)'} {url}"

    def _step_screenshot_refs(
        self,
        step: WebStepRecord,
        evidence_builder: "_EvidenceIdBuilder",
        *,
        state: "_EvidencePathState",
    ) -> list[EvaluationEvidenceRef]:
        refs: list[EvaluationEvidenceRef] = []
        terminal = step.terminal_verification
        if (
            terminal is not None
            and terminal.status == WebTerminalVerificationStatus.CAPTURED
            and terminal.previous_screenshot_path
            and terminal.previous_screenshot_path != step.screenshot_path
            and not state.has_initial
            and not state.has_path(terminal.previous_screenshot_path)
        ):
            ref = self._image_ref(
                evidence_builder=evidence_builder,
                step=step,
                path=terminal.previous_screenshot_path,
                label=f"Step {step.session_step} pre-verification screenshot",
                variant="initial_state",
                summary=(
                    "Earliest available clean screenshot for the round, captured before terminal "
                    "verification replaced the current screenshot."
                ),
                initial_state=True,
            )
            refs.append(ref)
            state.add(terminal.previous_screenshot_path, ref.id)
            state.has_initial = True
        if step.screenshot_path and not state.has_path(step.screenshot_path):
            is_terminal = bool(terminal and terminal.screenshot_path == step.screenshot_path)
            is_initial = not state.has_initial and not is_terminal
            variant = "terminal_verification" if is_terminal else "initial_state" if is_initial else "current"
            label = (
                f"Step {step.session_step} initial screenshot"
                if is_initial
                else f"Step {step.session_step} current screenshot"
            )
            summary = (
                "Earliest available clean screenshot for the round, captured after case setup."
                if is_initial
                else "Desktop-aligned current clean screenshot for the model step."
            )
            ref = self._image_ref(
                evidence_builder=evidence_builder,
                step=step,
                path=step.screenshot_path,
                label=label,
                variant=variant,
                summary=summary,
                pinned=is_terminal,
                pinned_reason="web_terminal_verification" if is_terminal else "",
                initial_state=is_initial,
            )
            refs.append(ref)
            state.add(step.screenshot_path, ref.id)
            state.has_initial = state.has_initial or is_initial
        return refs

    def _step_dom_refs(
        self,
        step: WebStepRecord,
        evidence_builder: "_EvidenceIdBuilder",
        *,
        state: "_EvidencePathState",
    ) -> list[EvaluationEvidenceRef]:
        refs: list[EvaluationEvidenceRef] = []
        terminal = step.terminal_verification
        if (
            terminal is not None
            and terminal.status == WebTerminalVerificationStatus.CAPTURED
            and terminal.previous_dom_snapshot_json_path
            and terminal.previous_dom_snapshot_json_path != step.dom_snapshot_json_path
            and not state.has_initial
            and not state.has_path(terminal.previous_dom_snapshot_json_path)
        ):
            ref = self._dom_ref(
                evidence_builder=evidence_builder,
                step=step,
                path=terminal.previous_dom_snapshot_json_path,
                text_path=terminal.previous_dom_snapshot_path,
                label=f"Step {step.session_step} pre-verification DOM",
                variant="initial_state",
                initial_state=True,
            )
            refs.append(ref)
            state.add(terminal.previous_dom_snapshot_json_path, ref.id)
            state.has_initial = True
        if step.dom_snapshot_json_path and not state.has_path(step.dom_snapshot_json_path):
            is_terminal = bool(terminal and terminal.dom_snapshot_json_path == step.dom_snapshot_json_path)
            is_initial = not state.has_initial and not is_terminal
            variant = "terminal_verification" if is_terminal else "initial_state" if is_initial else "current"
            label = f"Step {step.session_step} initial DOM" if is_initial else f"Step {step.session_step} current DOM"
            ref = self._dom_ref(
                evidence_builder=evidence_builder,
                step=step,
                path=step.dom_snapshot_json_path,
                text_path=step.dom_snapshot_path,
                label=label,
                variant=variant,
                initial_state=is_initial,
            )
            refs.append(ref)
            state.add(step.dom_snapshot_json_path, ref.id)
            state.has_initial = state.has_initial or is_initial
        return refs

    def _tool_results(
        self,
        step: WebStepRecord,
        evidence_builder: "_EvidenceIdBuilder",
        screenshot_refs: list[EvaluationEvidenceRef],
        image_state: "_EvidencePathState",
    ) -> list[JsonObject]:
        results: list[JsonObject] = []
        for artifact in step.tool_artifacts:
            image_ref_id = ""
            raw_image_ref_id = ""
            action_overlay_image_ref_id = ""
            som_image_ref_id = ""
            if artifact.selected_image_path:
                raw_image_ref_id = self._ensure_tool_image_ref(
                    artifact.raw_image_path,
                    artifact=artifact,
                    step=step,
                    evidence_builder=evidence_builder,
                    screenshot_refs=screenshot_refs,
                    image_state=image_state,
                    variant="raw",
                    label_suffix="raw result image",
                )
                action_overlay_image_ref_id = self._ensure_tool_image_ref(
                    artifact.action_overlay_image_path,
                    artifact=artifact,
                    step=step,
                    evidence_builder=evidence_builder,
                    screenshot_refs=screenshot_refs,
                    image_state=image_state,
                    variant="action_overlay",
                    label_suffix="action overlay image",
                )
                som_image_ref_id = self._ensure_tool_image_ref(
                    artifact.som_image_path,
                    artifact=artifact,
                    step=step,
                    evidence_builder=evidence_builder,
                    screenshot_refs=screenshot_refs,
                    image_state=image_state,
                    variant="som",
                    label_suffix="SoM result image",
                )
                image_ref_id = self._default_tool_image_ref(
                    raw_image_ref_id=raw_image_ref_id,
                    action_overlay_image_ref_id=action_overlay_image_ref_id,
                    som_image_ref_id=som_image_ref_id,
                )
            result: JsonObject = {
                "name": artifact.tool_name,
                "action": artifact.action,
                "status": artifact.status,
                "is_error": artifact.is_error,
                "text": artifact.text,
                "answer": str(artifact.metadata.get("answer") or ""),
                "tool_call_id": artifact.tool_call_id,
                "session_step": step.session_step,
                "round_step": step.step_index,
                "image_ref": image_ref_id,
                "raw_image_ref": raw_image_ref_id,
                "action_overlay_image_ref": action_overlay_image_ref_id,
                "som_image_ref": som_image_ref_id,
                "selected_image_kind": artifact.selected_image_kind.value,
                "default_image_ref_policy": "clean_then_action_overlay_then_som",
                "page_url": artifact.page_url,
                "page_title": artifact.page_title,
            }
            recovery = artifact.metadata.get("observation_recovery")
            if isinstance(recovery, Mapping) and recovery:
                result["observation_recovery"] = {str(key): value for key, value in recovery.items()}
            results.append(result)
        return results

    @staticmethod
    def _default_tool_image_ref(
        *,
        raw_image_ref_id: str,
        action_overlay_image_ref_id: str,
        som_image_ref_id: str,
    ) -> str:
        """Return the low-noise default image ref for evaluator model inputs.

        SoM is grounding/debug evidence, not the default current-state image.
        Role-specific refs remain available for tools that need them.
        """

        return raw_image_ref_id or action_overlay_image_ref_id or som_image_ref_id

    def _runtime_event_refs(
        self,
        steps: list[WebStepRecord],
        evidence_builder: "_EvidenceIdBuilder",
    ) -> list[EvaluationEvidenceRef]:
        """Return path-free trace evidence needed to grade answer-only Web tasks."""

        final_answer = (self.result.final_answer or "").strip()
        if not final_answer:
            return []
        terminal_step = steps[-1] if steps else None
        return [
            EvaluationEvidenceRef(
                id=evidence_builder.next_id(),
                kind=EvaluationEvidenceKind.TRACE,
                label="Final answer",
                summary=final_answer,
                session_step=terminal_step.session_step if terminal_step is not None else 0,
                round_step=terminal_step.step_index if terminal_step is not None else 0,
                pinned=True,
                selection_reason="web_final_answer",
                metadata={
                    "source": "web",
                    "event": "final_answer",
                    "terminal_status": self.result.terminal_status.value,
                    "status": self.result.status.value,
                },
            )
        ]

    def _ensure_tool_image_ref(
        self,
        path: str,
        *,
        artifact: WebToolResultArtifact,
        step: WebStepRecord,
        evidence_builder: "_EvidenceIdBuilder",
        screenshot_refs: list[EvaluationEvidenceRef],
        image_state: "_EvidencePathState",
        variant: str,
        label_suffix: str,
    ) -> str:
        if not path:
            return ""
        ref_id = image_state.ref_id_for(path)
        if ref_id:
            return ref_id
        tool_name = artifact.tool_name
        action = artifact.action
        selected_kind = artifact.selected_image_kind.value
        som_metadata = artifact.som_metadata
        image_ref = self._image_ref(
            evidence_builder=evidence_builder,
            step=step,
            path=path,
            label=f"Step {step.session_step} {tool_name} {label_suffix}",
            variant=variant,
            summary=artifact.post_action_observation_summary,
            initial_state=False,
            pinned=artifact.is_error,
            pinned_reason="web_tool_error_evidence" if artifact.is_error else "",
            metadata={
                "tool_name": tool_name,
                "action": action,
                "tool_status": artifact.status,
                "tool_is_error": artifact.is_error,
                "selected_image_kind": str(selected_kind),
                "som_metadata": som_metadata if variant == "som" else {},
                "tool_result_image_variant": variant,
            },
        )
        screenshot_refs.append(image_ref)
        image_state.add(path, image_ref.id)
        return image_ref.id

    def _image_ref(
        self,
        *,
        evidence_builder: "_EvidenceIdBuilder",
        step: WebStepRecord,
        path: str,
        label: str,
        variant: str,
        summary: str,
        pinned: bool = False,
        pinned_reason: str = "",
        initial_state: bool = False,
        metadata: JsonObject | None = None,
    ) -> EvaluationEvidenceRef:
        merged_metadata: JsonObject = {
            "variant": variant,
            "source": "web",
            "initial_state": initial_state,
        }
        if pinned:
            merged_metadata["pinned"] = True
            if pinned_reason:
                merged_metadata["pinned_reason"] = pinned_reason
        merged_metadata.update(metadata or {})
        return EvaluationEvidenceRef(
            id=evidence_builder.next_id(),
            kind=EvaluationEvidenceKind.IMAGE,
            label=label,
            summary=summary,
            path=path,
            session_step=step.session_step,
            round_step=step.step_index,
            pinned=pinned,
            metadata=merged_metadata,
        )

    def _dom_ref(
        self,
        *,
        evidence_builder: "_EvidenceIdBuilder",
        step: WebStepRecord,
        path: str,
        text_path: str,
        label: str,
        variant: str,
        initial_state: bool = False,
    ) -> EvaluationEvidenceRef:
        page = step.observation_artifact
        return EvaluationEvidenceRef(
            id=evidence_builder.next_id(),
            kind=EvaluationEvidenceKind.DOM,
            label=label,
            summary=f"{page.page_title if page else ''} {page.page_url if page else step.url}".strip(),
            path=path,
            session_step=step.session_step,
            round_step=step.step_index,
            metadata={
                "variant": variant,
                "source": "web",
                "text_path": text_path,
                "terminal_verification": variant == "terminal_verification",
                "initial_state": initial_state,
            },
        )


@dataclass(slots=True)
class _EvidencePathState:
    """Track desktop-style evidence de-duplication and initial-state selection."""

    seen_paths: set[str] = field(default_factory=set)
    ref_ids_by_path: dict[str, str] = field(default_factory=dict)
    has_initial: bool = False

    def has_path(self, path: str) -> bool:
        return path in self.seen_paths

    def add(self, path: str, ref_id: str) -> None:
        self.seen_paths.add(path)
        self.ref_ids_by_path[path] = ref_id

    def ref_id_for(self, path: str) -> str:
        return self.ref_ids_by_path.get(path, "")


class _EvidenceIdBuilder:
    """Generate stable bundle-local evidence ids."""

    def __init__(self) -> None:
        self._index = 0

    def next_id(self) -> str:
        self._index += 1
        return f"E{self._index:03d}"
