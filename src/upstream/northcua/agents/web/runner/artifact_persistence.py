"""Artifact and trace persistence for web-agent runs."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import logging
import re
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from northcua.agentinfra.tracing.log_writer import LLMTraceLogWriter
from northcua.agentkernel.tracing.capture import TraceCaptureService
from northcua.agentkernel.tracing.state import TraceCaptureState
from northcua.app.logging import log_runtime_event
from northcua.core.config import WebAgentConfig
from northcua.core.json_types import JsonObject
from northcua.core.models import LLMTraceRecord

from ..artifacts import WebArtifactStore
from ..backend import WebActionBackend, WebActionBackendError
from ..domain.evaluation_artifacts import (
    WebObservationArtifact,
    WebObservationArtifactRole,
    WebTerminalVerificationRecord,
    WebTerminalVerificationStatus,
    WebToolImageArtifactEvent,
    WebToolInvocationArtifactEvent,
    WebToolResultArtifact,
)
from ..domain.observations import WebObservationContext, WebScreenshotObservation
from ..domain.results import WebAgentResult, WebStepRecord
from ..observation.artifact_dom import WebDomArtifactProjector
from .artifact_records import WebArtifactRecordProjector
from .remote_artifacts import WebRemoteToolArtifactSink

if TYPE_CHECKING:
    from .action_resolution import ResolvedWebAgentAction

logger = logging.getLogger(__name__)
_INLINE_IMAGE_URL_KEYS = {
    "data_url",
    "image_url",
    "screenshot_url",
    "raw_image_url",
    "action_overlay_image_url",
    "som_image_url",
}


class WebAgentArtifactPersistence:
    """Persist DOM, screenshots, step traces, LLM traces, and final results."""

    _PNG_DATA_URL_PREFIX = "data:image/png;base64,"
    _DATA_IMAGE_REDACTION = "[redacted data URL; see screenshot artifact path]"
    _DATA_IMAGE_PATTERN = re.compile(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=_-]+", re.IGNORECASE)

    def __init__(self, *, artifact_store: WebArtifactStore, config: WebAgentConfig) -> None:
        self.artifact_store = artifact_store
        self.config = config
        self._record_projector = WebArtifactRecordProjector()
        self._dom_projector = WebDomArtifactProjector()

    async def persist_observation_artifacts(
        self,
        *,
        client: WebActionBackend,
        step_index: int,
        observation: WebObservationContext,
    ) -> dict[str, Path]:
        """Persist per-step DOM and screenshot artifacts independent of prompt policy.

        Args:
            client: Browser client used to capture a screenshot when the
                observation does not already include one.
            step_index: One-based step index used in artifact filenames.
            observation: Snapshot/screenshot data collected for this step.

        Returns:
            Mapping of artifact field names to paths written for this step.
        """

        dom_path = self.artifact_store.dom_snapshot_path(step_index)
        dom_json_path = self.artifact_store.dom_snapshot_json_path(step_index)
        self.artifact_store.write_text(dom_path, self._dom_projector.text_for_snapshot(observation.snapshot))
        self.artifact_store.write_json(dom_json_path, observation.snapshot.model_dump(mode="json"))
        paths: dict[str, Path] = {
            "dom_snapshot_path": dom_path,
            "dom_snapshot_json_path": dom_json_path,
        }
        try:
            screenshot = observation.screenshot or await client.screenshot()
            saved_raw_path = self.persist_step_screenshot(step_index=step_index, screenshot=screenshot)
            saved_annotated_path = self.persist_step_screenshot(
                step_index=step_index,
                screenshot=screenshot,
                annotated=True,
            )
            if saved_raw_path is not None:
                paths["screenshot_path"] = saved_raw_path
            if saved_annotated_path is not None:
                paths["annotated_screenshot_path"] = saved_annotated_path
            if saved_annotated_path is not None:
                screenshot.path = str(saved_annotated_path)
            elif saved_raw_path is not None:
                screenshot.path = str(saved_raw_path)
            observation.screenshot = screenshot
        except (WebActionBackendError, ValueError, binascii.Error) as exc:
            log_runtime_event(
                logger,
                "web_agent_step_screenshot_persist_failed",
                level=logging.WARNING,
                session_id=self.artifact_store.session_id,
                step_index=step_index,
                error=str(exc),
                error_type=type(exc).__name__,
            )
        return paths

    def persist_step(self, step_record: WebStepRecord) -> None:
        """Append one step record with bulky inline screenshot data redacted.

        Args:
            step_record: Structured step record to append to the trace JSONL.
        """

        self.artifact_store.append_jsonl(
            self.artifact_store.trace_path,
            self._redact_data_urls(step_record.model_dump(mode="json", exclude_none=True)),
        )

    def replace_step_trace(self, steps: list[WebStepRecord]) -> None:
        """Merge updated step records into the existing Web step trace.

        This is used by terminal verification after it overwrites the final
        step's current observation. Existing rows from previous rounds are
        preserved, and rows are replaced by stable ``session_step``.
        """

        trace_path = self.artifact_store.trace_path
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        replacements = {
            self._trace_step_key(step.model_dump(mode="json", exclude_none=True)): self._redact_data_urls(
                step.model_dump(mode="json", exclude_none=True)
            )
            for step in steps
        }
        rows: list[JsonObject] = []
        if trace_path.exists():
            for line in trace_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                key = self._trace_step_key(payload)
                rows.append(replacements.pop(key) if key in replacements else payload)
        rows.extend(replacements[key] for key in sorted(replacements))
        with trace_path.open("w", encoding="utf-8") as handle:
            for payload in rows:
                handle.write(f"{json.dumps(payload, ensure_ascii=False, sort_keys=True)}\n")

    def count_persisted_steps(self) -> int:
        """Return the number of step records already persisted for this session.

        Returns:
            Existing structured step count, or zero when the trace file has not
            been created yet.
        """

        trace_path = self.artifact_store.trace_path
        if not trace_path.exists():
            return 0
        return sum(1 for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip())

    def max_persisted_round_num(self) -> int:
        """Return the highest round number already persisted in this session."""

        trace_path = self.artifact_store.trace_path
        if not trace_path.exists():
            return 0
        max_round_num = 0
        for line in trace_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            round_num = payload.get("round_num")
            if isinstance(round_num, int) and round_num > max_round_num:
                max_round_num = round_num
        return max_round_num

    def iter_screenshot_artifact_paths(self) -> list[Path]:
        """Return persisted screenshot image artifacts in stable creation order."""

        screenshots_dir = self.artifact_store.screenshots_dir
        if not screenshots_dir.exists():
            return []
        paths = [path for path in screenshots_dir.rglob("*.png") if path.is_file()]
        return sorted(paths, key=lambda path: (path.stat().st_mtime_ns, path.name))

    def persist_llm_trace(
        self,
        trace_state: TraceCaptureState,
        *,
        session_id: str,
        session_step: int,
        round_num: int,
        round_step: int,
        request: str,
        runtime_cursor: JsonObject | None = None,
    ) -> None:
        """Append one desktop-style wrapped LLM trace with raw prompt/tool images.

        Args:
            trace_state: Captured provider request/response and tool trace.
            session_id: Active web-agent session id.
            session_step: One-based step index across the session.
            round_num: One-based user request round number.
            round_step: One-based step index within the round.
            request: User task text for this round.
            runtime_cursor: Optional shared runtime cursor payload for
                audit/debugging.
        """

        if trace_state.llm_config is None:
            llm_trace = LLMTraceRecord(
                request=trace_state.request_payload,
                response=trace_state.response_payload,
                usage=trace_state.usage,
                stop_reason=trace_state.stop_reason,
                cost=trace_state.cost,
                reasoning=trace_state.reasoning,
                reasoning_encrypted_content=trace_state.reasoning_encrypted_content,
                assistant_text=trace_state.assistant_text,
                assistant_tool_calls=list(trace_state.assistant_tool_calls),
                tool_executions=list(trace_state.tool_executions),
            )
        else:
            llm_trace = TraceCaptureService(trace_state.llm_config).build_trace_record(trace_state)
        llm_trace_payload = llm_trace.model_dump(mode="json", exclude_none=True)
        redacted_runtime_cursor = self._redact_data_urls(runtime_cursor) if runtime_cursor is not None else None
        LLMTraceLogWriter(self.artifact_store.llm_trace_log_path).log_web_trace(
            session_id=session_id,
            session_step=session_step,
            round_num=round_num,
            round_step=round_step,
            request=request,
            llm_trace=llm_trace_payload,
            runtime_cursor=redacted_runtime_cursor,
        )

    def persist_result(self, result: WebAgentResult) -> None:
        """Write the final structured result with data URLs redacted.

        Args:
            result: Final web-agent run result.
        """

        payload = result.model_dump(mode="json", exclude_none=True)
        payload["terminal_decision"] = result.to_terminal_decision().to_json()
        self.artifact_store.write_json(
            self.artifact_store.root / "rounds" / f"round_{result.round_num:04d}" / "result.json",
            self._redact_data_urls(payload),
        )
        self.artifact_store.write_json(
            self.artifact_store.result_path,
            self._redact_data_urls(payload),
        )

    def build_observation_artifact(
        self,
        *,
        observation: WebObservationContext,
        paths: Mapping[str, Path | str],
        role: WebObservationArtifactRole = WebObservationArtifactRole.CURRENT,
    ) -> WebObservationArtifact:
        """Build the typed current-observation artifact for one web step."""

        return self._record_projector.build_observation_artifact(
            observation=observation,
            paths=paths,
            role=role,
        )

    def build_tool_result_artifacts(
        self,
        *,
        actions: list["ResolvedWebAgentAction"],
        action_screenshot_paths: list[Path],
        artifact_invocations: list[WebToolInvocationArtifactEvent] | None = None,
        artifact_events: list[WebToolImageArtifactEvent] | None = None,
        session_step: int | None = None,
    ) -> list[WebToolResultArtifact]:
        """Build desktop-style tool-result artifacts for web action outputs."""

        action_screenshot_paths = [
            *action_screenshot_paths,
            *self._materialize_inline_tool_result_images(
                actions=actions,
                candidate_paths=action_screenshot_paths,
                session_step=session_step,
            ),
        ]
        return self._record_projector.build_tool_result_artifacts(
            actions=actions,
            action_screenshot_paths=action_screenshot_paths,
            artifact_invocations=artifact_invocations,
            artifact_events=artifact_events,
        )

    def _materialize_inline_tool_result_images(
        self,
        *,
        actions: list["ResolvedWebAgentAction"],
        candidate_paths: list[Path],
        session_step: int | None,
    ) -> list[Path]:
        """Persist inline remote tool images when no controller-local path exists.

        Remote device execution can return a normal model-visible image block
        while the controller never observes a newly created screenshot file.
        Desktop-style replay still needs a local artifact path for the tool
        result, so we materialize the data URL in the runner artifact store and
        let ``WebArtifactRecordProjector`` attribute it by image digest.
        """

        known_digests = self._image_file_digests(candidate_paths)
        materialized_paths: list[Path] = []
        materialized_digests: set[str] = set()
        sink = WebRemoteToolArtifactSink(
            artifact_store=self.artifact_store,
            config=self.config,
            session_step=session_step or 0,
        )
        for action in actions:
            output = action.tool_output if isinstance(action.tool_output, dict) else {}
            if self._record_projector.existing_image_paths_from_output(output):
                continue
            for key, data_url in self._iter_tool_output_image_data_urls(output):
                try:
                    digest = self._image_data_url_digest(data_url)
                except (ValueError, binascii.Error) as exc:
                    log_runtime_event(
                        logger,
                        "web_agent_remote_tool_image_materialize_failed",
                        level=logging.WARNING,
                        session_id=self.artifact_store.session_id,
                        tool_name=action.tool_name,
                        tool_call_id=action.tool_call_id,
                        image_key=key,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    continue
                if not digest or digest in known_digests or digest in materialized_digests:
                    continue
                try:
                    path_text = sink.materialize_image_data_url(
                        tool_name=action.tool_name,
                        key=key,
                        data_url=data_url,
                        metadata={
                            **output,
                            "tool_call_id": action.tool_call_id or "",
                            "tool_name": action.tool_name,
                            "action": action.action.value,
                        },
                    )
                except (ValueError, binascii.Error) as exc:
                    log_runtime_event(
                        logger,
                        "web_agent_remote_tool_image_materialize_failed",
                        level=logging.WARNING,
                        session_id=self.artifact_store.session_id,
                        tool_name=action.tool_name,
                        tool_call_id=action.tool_call_id,
                        image_key=key,
                        error=str(exc),
                        error_type=type(exc).__name__,
                    )
                    continue
                if not path_text:
                    continue
                path = Path(path_text)
                materialized_paths.append(path)
                materialized_digests.add(digest)
        return materialized_paths

    def _image_file_digests(self, paths: list[Path]) -> set[str]:
        digests: set[str] = set()
        for path in paths:
            try:
                if path.is_file():
                    digests.add(hashlib.sha256(path.read_bytes()).hexdigest())
            except OSError:
                continue
        return digests

    def _image_data_url_digest(self, data_url: str) -> str:
        decoded = self._decode_image_data_url(data_url)
        if decoded is None:
            return ""
        return hashlib.sha256(decoded).hexdigest()

    def _decode_image_data_url(self, data_url: str) -> bytes | None:
        match = re.fullmatch(r"data:image/[A-Za-z0-9.+-]+;base64,([A-Za-z0-9+/=]+)", data_url, flags=re.IGNORECASE)
        if match is None:
            return None
        decoded = base64.b64decode(match.group(1), validate=True)
        if len(decoded) > self.config.max_screenshot_bytes:
            raise ValueError(
                f"Remote image exceeds web.max_screenshot_bytes={self.config.max_screenshot_bytes}: {len(decoded)}"
            )
        return decoded

    def _iter_tool_output_image_data_urls(self, value: object) -> list[tuple[str, str]]:
        image_urls: list[tuple[str, str]] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key)
                if key_text in _INLINE_IMAGE_URL_KEYS and isinstance(item, str) and self._is_image_data_url(item):
                    image_urls.append((key_text, item))
                    continue
                image_urls.extend(self._iter_tool_output_image_data_urls(item))
        elif isinstance(value, list):
            for item in value:
                image_urls.extend(self._iter_tool_output_image_data_urls(item))
        return image_urls

    @staticmethod
    def _is_image_data_url(value: str) -> bool:
        return bool(re.fullmatch(r"data:image/[A-Za-z0-9.+-]+;base64,[A-Za-z0-9+/=]+", value, flags=re.IGNORECASE))

    async def attach_terminal_verification(
        self,
        *,
        client: WebActionBackend,
        step_record: WebStepRecord,
        observation: WebObservationContext,
        reason: str,
        runtime_mode: str = "local",
    ) -> WebTerminalVerificationRecord:
        """Replace the final step current observation with terminal evidence."""

        previous_screenshot_path = step_record.screenshot_path
        previous_dom_path = step_record.dom_snapshot_path
        previous_dom_json_path = step_record.dom_snapshot_json_path
        try:
            artifact_step_index = step_record.session_step or step_record.step_index
            screenshot = observation.screenshot or await client.screenshot()
            screenshot_path = self._persist_terminal_screenshot(
                step_index=artifact_step_index,
                screenshot=screenshot,
            )
            if screenshot_path is None:
                raise ValueError("Terminal verification screenshot was not available.")
            dom_path = self.artifact_store.terminal_dom_snapshot_path(artifact_step_index)
            dom_json_path = self.artifact_store.terminal_dom_snapshot_json_path(artifact_step_index)
            self.artifact_store.write_text(dom_path, self._dom_projector.text_for_snapshot(observation.snapshot))
            self.artifact_store.write_json(dom_json_path, observation.snapshot.model_dump(mode="json"))
            terminal_paths: dict[str, Path] = {
                "dom_snapshot_path": dom_path,
                "dom_snapshot_json_path": dom_json_path,
                "screenshot_path": screenshot_path,
            }
            step_record.screenshot_path = str(screenshot_path)
            step_record.dom_snapshot_path = str(dom_path)
            step_record.dom_snapshot_json_path = str(dom_json_path)
            step_record.url = observation.page_url
            step_record.observation = observation.snapshot
            step_record.observation_artifact = self.build_observation_artifact(
                observation=observation,
                paths=terminal_paths,
                role=WebObservationArtifactRole.TERMINAL_VERIFICATION,
            )
            metadata = dict(step_record.observation_metadata)
            metadata.update(
                {
                    "web_current_observation_role": WebObservationArtifactRole.TERMINAL_VERIFICATION.value,
                    "web_current_screenshot_path": step_record.screenshot_path,
                    "web_current_dom_snapshot_path": step_record.dom_snapshot_path,
                    "web_current_dom_snapshot_json_path": step_record.dom_snapshot_json_path,
                    "web_current_observation_id": observation.observation_id,
                    "web_current_page_url": observation.page_url,
                    "web_current_page_title": observation.page_title,
                    "web_current_ref_count": len(observation.snapshot.elements),
                    "web_current_settled": observation.settled,
                    "web_current_settle_reason": observation.settle_reason,
                    "web_terminal_verification_status": WebTerminalVerificationStatus.CAPTURED.value,
                    "web_terminal_verification_reason": reason,
                    "web_terminal_verification_source": "runtime_finalize",
                    "web_terminal_previous_screenshot_path": previous_screenshot_path,
                    "web_terminal_previous_dom_snapshot_path": previous_dom_path,
                    "web_terminal_previous_dom_snapshot_json_path": previous_dom_json_path,
                }
            )
            step_record.observation_metadata = metadata
            record = WebTerminalVerificationRecord(
                status=WebTerminalVerificationStatus.CAPTURED,
                reason=reason,
                source="runtime_finalize",
                runtime_mode=runtime_mode,
                previous_screenshot_path=previous_screenshot_path,
                screenshot_path=step_record.screenshot_path,
                previous_dom_snapshot_path=previous_dom_path,
                dom_snapshot_path=step_record.dom_snapshot_path,
                previous_dom_snapshot_json_path=previous_dom_json_path,
                dom_snapshot_json_path=step_record.dom_snapshot_json_path,
                page_url=observation.page_url,
                page_title=observation.page_title,
            )
            step_record.terminal_verification = record
            return record
        except Exception as exc:
            record = self.mark_terminal_verification_failed(
                step_record=step_record,
                reason=reason,
                error=exc,
                runtime_mode=runtime_mode,
            )
            log_runtime_event(
                logger,
                "web_agent_terminal_verification_failed",
                level=logging.WARNING,
                session_id=self.artifact_store.session_id,
                step_index=step_record.step_index,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return record

    def mark_terminal_verification_failed(
        self,
        *,
        step_record: WebStepRecord,
        reason: str,
        error: Exception,
        runtime_mode: str = "local",
    ) -> WebTerminalVerificationRecord:
        """Record a failed terminal verification without adding artifacts."""

        record = WebTerminalVerificationRecord(
            status=WebTerminalVerificationStatus.FAILED,
            reason=reason,
            source="runtime_finalize",
            runtime_mode=runtime_mode,
            previous_screenshot_path=step_record.screenshot_path,
            previous_dom_snapshot_path=step_record.dom_snapshot_path,
            previous_dom_snapshot_json_path=step_record.dom_snapshot_json_path,
            error=str(error),
        )
        step_record.terminal_verification = record
        metadata = dict(step_record.observation_metadata)
        metadata.update(
            {
                "web_terminal_verification_status": WebTerminalVerificationStatus.FAILED.value,
                "web_terminal_verification_reason": reason,
                "web_terminal_verification_error": str(error),
            }
        )
        step_record.observation_metadata = metadata
        return record

    def persist_step_screenshot(
        self,
        *,
        step_index: int,
        screenshot: WebScreenshotObservation,
        annotated: bool = False,
    ) -> Path | None:
        """Persist one screenshot while enforcing max decoded size.

        Args:
            step_index: One-based step index used in the target filename.
            screenshot: Screenshot metadata containing a data URL or path.

        Returns:
            Path of the persisted screenshot, or ``None`` when no image data is
            available.
        """

        target_path = self.artifact_store.screenshot_path(step_index, annotated=annotated)
        preferred_path = self._preferred_screenshot_source_path(screenshot, annotated=annotated)
        if preferred_path:
            source_path = Path(preferred_path)
            if source_path.exists():
                self._validate_file_size(source_path)
                if source_path != target_path:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_path, target_path)
                    if annotated:
                        self._persist_screenshot_annotation_sidecar(
                            screenshot=screenshot,
                            source_path=source_path,
                            target_path=target_path,
                        )
                    return target_path
                if annotated:
                    self._persist_screenshot_annotation_sidecar(
                        screenshot=screenshot,
                        source_path=source_path,
                        target_path=target_path,
                    )
                return source_path
        if screenshot.data_url and not annotated:
            decoded = self._decode_png_data_url(screenshot.data_url)
            if decoded is not None:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(decoded)
                return target_path
        if screenshot.path and not annotated:
            source_path = Path(screenshot.path)
            if source_path.exists():
                self._validate_file_size(source_path)
                if source_path != target_path:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(source_path, target_path)
                    return target_path
                return source_path
        return None

    def _persist_screenshot_annotation_sidecar(
        self,
        *,
        screenshot: WebScreenshotObservation,
        source_path: Path,
        target_path: Path,
    ) -> None:
        """Keep a copied annotated screenshot paired with its SoM sidecar."""

        target_sidecar = target_path.with_suffix(".annotation.json")
        source_sidecar = source_path.with_suffix(".annotation.json")
        if source_sidecar.exists():
            target_sidecar.parent.mkdir(parents=True, exist_ok=True)
            if source_sidecar.resolve() != target_sidecar.resolve():
                shutil.copyfile(source_sidecar, target_sidecar)
            return
        if screenshot.annotation is None:
            return
        target_sidecar.parent.mkdir(parents=True, exist_ok=True)
        target_sidecar.write_text(
            json.dumps(screenshot.annotation.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _persist_terminal_screenshot(
        self,
        *,
        step_index: int,
        screenshot: WebScreenshotObservation,
    ) -> Path | None:
        target_path = self.artifact_store.terminal_screenshot_path(step_index)
        preferred_path = self._preferred_screenshot_source_path(screenshot, annotated=False)
        if preferred_path:
            source_path = Path(preferred_path)
            if source_path.exists():
                self._validate_file_size(source_path)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, target_path)
                return target_path
        if screenshot.data_url:
            decoded = self._decode_png_data_url(screenshot.data_url)
            if decoded is not None:
                target_path.parent.mkdir(parents=True, exist_ok=True)
                target_path.write_bytes(decoded)
                return target_path
        if screenshot.path:
            source_path = Path(screenshot.path)
            if source_path.exists():
                self._validate_file_size(source_path)
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source_path, target_path)
                return target_path
        return None

    def _preferred_screenshot_source_path(self, screenshot: WebScreenshotObservation, *, annotated: bool) -> str:
        annotation = screenshot.annotation
        if annotation is not None:
            if annotated and annotation.overlay_screenshot_path:
                return annotation.overlay_screenshot_path
            if not annotated and annotation.source_screenshot_path:
                return annotation.source_screenshot_path
        if annotated:
            return ""
        return screenshot.path

    @staticmethod
    def _trace_step_key(payload: Mapping[str, object]) -> int:
        session_step = payload.get("session_step")
        if isinstance(session_step, int) and session_step > 0:
            return session_step
        step_index = payload.get("step_index")
        return step_index if isinstance(step_index, int) else 0

    def _decode_png_data_url(self, data_url: str) -> bytes | None:
        if not data_url.startswith(self._PNG_DATA_URL_PREFIX):
            return None
        decoded = base64.b64decode(data_url.removeprefix(self._PNG_DATA_URL_PREFIX), validate=True)
        if len(decoded) > self.config.max_screenshot_bytes:
            raise ValueError(
                f"Screenshot exceeds web.max_screenshot_bytes={self.config.max_screenshot_bytes}: {len(decoded)}"
            )
        return decoded

    def _validate_file_size(self, path: Path) -> None:
        size = path.stat().st_size
        if size > self.config.max_screenshot_bytes:
            raise ValueError(f"Screenshot exceeds web.max_screenshot_bytes={self.config.max_screenshot_bytes}: {size}")

    def _redact_data_urls(self, payload: object) -> JsonObject:
        redacted = self._redact_value(payload)
        return redacted if isinstance(redacted, dict) else {"value": redacted}

    def _redact_value(self, value: object) -> object:
        if isinstance(value, str):
            return self._DATA_IMAGE_PATTERN.sub(self._DATA_IMAGE_REDACTION, value)
        if isinstance(value, Mapping):
            return {str(key): self._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact_value(item) for item in value]
        return value
