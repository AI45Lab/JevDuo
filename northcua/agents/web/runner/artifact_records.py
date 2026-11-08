"""Typed Web artifact record projection helpers."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TYPE_CHECKING, cast

from northcua.core.json_types import JsonObject

from ..domain.evaluation_artifacts import (
    WebObservationArtifact,
    WebObservationArtifactRole,
    WebToolArtifactAttributionSource,
    WebToolArtifactImageKind,
    WebToolImageArtifactEvent,
    WebToolInvocationArtifactEvent,
    WebToolResultArtifact,
    WebToolResultArtifactHint,
)
from ..domain.observations import WebObservationContext

if TYPE_CHECKING:
    from .action_resolution import ResolvedWebAgentAction


class WebArtifactRecordProjector:
    """Build typed evaluator/replay records from persisted Web artifacts."""

    _IMAGE_DATA_URL_PATTERN = re.compile(
        r"^data:image/[A-Za-z0-9.+-]+;base64,([A-Za-z0-9+/=]+)$",
        re.IGNORECASE,
    )
    _PATH_KEYS = {
        "path",
        "screenshot_path",
        "image_path",
        "raw_image_path",
        "action_overlay_image_path",
        "som_image_path",
        "source_screenshot_path",
        "overlay_screenshot_path",
    }
    _IMAGE_URL_KEYS = {
        "data_url",
        "image_url",
        "screenshot_url",
        "raw_image_url",
        "action_overlay_image_url",
        "som_image_url",
    }

    def build_observation_artifact(
        self,
        *,
        observation: WebObservationContext,
        paths: Mapping[str, Path | str],
        role: WebObservationArtifactRole = WebObservationArtifactRole.CURRENT,
    ) -> WebObservationArtifact:
        """Build the typed current-observation artifact for one Web step."""

        snapshot = observation.snapshot
        screenshot_path = str(paths.get("screenshot_path") or "")
        annotated_screenshot_path = str(paths.get("annotated_screenshot_path") or "")
        raw_snapshot = snapshot.raw if isinstance(snapshot.raw, dict) else {}
        viewport = raw_snapshot.get("viewport")
        return WebObservationArtifact(
            role=role,
            screenshot_path=screenshot_path,
            annotated_screenshot_path=annotated_screenshot_path,
            dom_snapshot_path=str(paths.get("dom_snapshot_path") or ""),
            dom_snapshot_json_path=str(paths.get("dom_snapshot_json_path") or ""),
            page_url=observation.page_url,
            page_title=observation.page_title,
            observation_id=observation.observation_id,
            settled=observation.settled,
            settle_reason=observation.settle_reason,
            ref_count=len(snapshot.elements),
            viewport=cast(JsonObject, viewport) if isinstance(viewport, dict) else {},
            metadata={
                "source": "web_current_observation",
                "has_clean_screenshot": bool(screenshot_path),
                "has_annotated_screenshot": bool(annotated_screenshot_path),
                "has_dom_text": bool(paths.get("dom_snapshot_path")),
                "has_dom_json": bool(paths.get("dom_snapshot_json_path")),
            },
        )

    def build_tool_result_artifacts(
        self,
        *,
        actions: list["ResolvedWebAgentAction"],
        action_screenshot_paths: list[Path],
        artifact_invocations: list[WebToolInvocationArtifactEvent] | None = None,
        artifact_events: list[WebToolImageArtifactEvent] | None = None,
    ) -> list[WebToolResultArtifact]:
        """Build desktop-style tool-result artifacts for Web action outputs."""

        if not actions:
            return []
        hints = self._build_action_artifact_hints(
            actions=actions,
            action_screenshot_paths=action_screenshot_paths,
            artifact_invocations=artifact_invocations or [],
            artifact_events=artifact_events or [],
        )
        artifacts: list[WebToolResultArtifact] = []
        for index, action in enumerate(actions, start=1):
            output = action.tool_output if isinstance(action.tool_output, dict) else {}
            hint = hints.get(index - 1)
            selected_kind = hint.selected_image_kind if hint is not None else WebToolArtifactImageKind.NONE
            raw_path = hint.raw_image_path if hint is not None else ""
            action_overlay_path = hint.action_overlay_image_path if hint is not None else ""
            som_path = hint.som_image_path if hint is not None else ""
            selected_path = hint.selected_image_path if hint is not None else ""
            observation = output.get("observation") if isinstance(output, dict) else None
            page = observation.get("page") if isinstance(observation, dict) else None
            screenshot = observation.get("screenshot") if isinstance(observation, dict) else None
            recovery = output.get("observation_recovery")
            observation_recovered = isinstance(recovery, dict) and recovery.get("status") == "recovered"
            recovery_metadata = self._recovery_metadata(recovery)
            image_count = int(bool(raw_path)) + int(bool(action_overlay_path)) + int(bool(som_path))
            attribution_source = hint.source.value if hint is not None else WebToolArtifactAttributionSource.NONE.value
            hint_metadata = hint.metadata if hint is not None else {}
            artifacts.append(
                WebToolResultArtifact(
                    tool_name=action.tool_name,
                    tool_call_id=action.tool_call_id or f"{action.tool_name}:{index}",
                    action=action.action.value,
                    status=str(output.get("status") or ("error" if action.is_error else "ok")),
                    is_error=action.is_error,
                    text=str(output.get("text") or output.get("answer") or output.get("error") or ""),
                    selected_image_kind=selected_kind,
                    raw_image_path=raw_path,
                    action_overlay_image_path=action_overlay_path,
                    som_image_path=som_path,
                    selected_image_path=selected_path,
                    post_action_observation_summary=self._observation_summary(observation),
                    page_url=str(page.get("url") or "") if isinstance(page, dict) else "",
                    page_title=str(page.get("title") or "") if isinstance(page, dict) else "",
                    som_metadata=self._som_metadata(screenshot),
                    metadata={
                        "source": "web_tool_result",
                        "image_count": image_count,
                        "attribution_source": attribution_source,
                        "observation_recovered": observation_recovered,
                        "observation_recovery": recovery_metadata,
                        **hint_metadata,
                        "answer": str(output.get("answer") or ""),
                    },
                )
            )
        return artifacts

    @staticmethod
    def _recovery_metadata(value: object) -> JsonObject:
        """Return the bounded recovery outcome needed by evaluator and viewer."""

        if not isinstance(value, dict):
            return {}
        return {
            key: value.get(key)
            for key in (
                "status",
                "source",
                "reason",
                "trigger_code",
                "retryable",
                "refs_invalidated",
                "hint",
            )
            if key in value
        }

    def existing_image_paths_from_output(self, output: JsonObject) -> list[Path]:
        """Return existing local image paths explicitly referenced by a tool output."""

        return self._existing_image_paths_from_output(output)

    def _build_action_artifact_hints(
        self,
        *,
        actions: list["ResolvedWebAgentAction"],
        action_screenshot_paths: list[Path],
        artifact_invocations: list[WebToolInvocationArtifactEvent],
        artifact_events: list[WebToolImageArtifactEvent],
    ) -> dict[int, WebToolResultArtifactHint]:
        candidate_paths = [path for path in action_screenshot_paths if path.exists()]
        digest_paths = self._digest_paths(candidate_paths)
        hints: dict[int, WebToolResultArtifactHint] = self._hints_from_artifact_events(
            actions=actions,
            artifact_invocations=artifact_invocations,
            artifact_events=artifact_events,
        )
        for index, action in enumerate(actions):
            existing = hints.get(index)
            output = action.tool_output if isinstance(action.tool_output, dict) else {}
            explicit_paths = self._existing_image_paths_from_output(output)
            source = (
                WebToolArtifactAttributionSource.EXPLICIT_PATH
                if explicit_paths
                else WebToolArtifactAttributionSource.NONE
            )
            if not explicit_paths:
                explicit_paths = self._image_paths_from_data_urls(output, digest_paths)
                if explicit_paths:
                    source = WebToolArtifactAttributionSource.IMAGE_HASH
            if not explicit_paths and existing is not None:
                continue
            raw_path, action_overlay_path, som_path = self._split_action_image_paths(
                [str(path) for path in explicit_paths]
            )
            if som_path and not raw_path:
                raw_path = self._source_path_for_som(som_path)
            if existing is not None:
                raw_path = raw_path or existing.raw_image_path
                action_overlay_path = action_overlay_path or existing.action_overlay_image_path
                som_path = som_path or existing.som_image_path
                if source is WebToolArtifactAttributionSource.NONE:
                    source = existing.source
            selected_kind, selected_path = self._default_selected_image(
                raw_path=raw_path,
                action_overlay_path=action_overlay_path,
                som_path=som_path,
            )
            if selected_kind is WebToolArtifactImageKind.NONE:
                continue
            hints[index] = WebToolResultArtifactHint(
                action_index=index,
                tool_name=action.tool_name,
                tool_call_id=action.tool_call_id,
                source=source,
                selected_image_kind=selected_kind,
                raw_image_path=raw_path,
                action_overlay_image_path=action_overlay_path,
                som_image_path=som_path,
                selected_image_path=selected_path,
                metadata={
                    **(existing.metadata if existing is not None else {}),
                    "candidate_path_count": len(candidate_paths),
                },
            )
        return hints

    def _hints_from_artifact_events(
        self,
        *,
        actions: list["ResolvedWebAgentAction"],
        artifact_invocations: list[WebToolInvocationArtifactEvent],
        artifact_events: list[WebToolImageArtifactEvent],
    ) -> dict[int, WebToolResultArtifactHint]:
        if len(artifact_invocations) != len(actions):
            return {}
        events_by_invocation = self._artifact_events_by_invocation(artifact_events)
        hints: dict[int, WebToolResultArtifactHint] = {}
        for action_index, action in enumerate(actions):
            if action_index >= len(artifact_invocations):
                continue
            invocation = artifact_invocations[action_index]
            if invocation.action and invocation.action != action.action.value:
                continue
            events = events_by_invocation.get(invocation.invocation_id, [])
            if not events:
                continue
            raw_path, action_overlay_path, som_path = self._split_action_image_paths(
                [event.raw_image_path for event in events if event.raw_image_path]
                + [event.action_overlay_image_path for event in events if event.action_overlay_image_path]
                + [event.som_image_path for event in events if event.som_image_path]
                + [event.selected_image_path for event in events if event.selected_image_path]
            )
            if som_path and not raw_path:
                raw_path = self._source_path_for_som(som_path)
            selected_kind, selected_path = self._default_selected_image(
                raw_path=raw_path,
                action_overlay_path=action_overlay_path,
                som_path=som_path,
            )
            if selected_kind is WebToolArtifactImageKind.NONE:
                continue
            hints[action_index] = WebToolResultArtifactHint(
                action_index=action_index,
                tool_name=action.tool_name,
                tool_call_id=action.tool_call_id,
                source=events[-1].source,
                selected_image_kind=selected_kind,
                raw_image_path=raw_path,
                action_overlay_image_path=action_overlay_path,
                som_image_path=som_path,
                selected_image_path=selected_path,
                metadata={
                    "event_count": len(events),
                    "invocation_id": invocation.invocation_id,
                    "invocation_sequence": invocation.sequence,
                    "candidate_path_count": len(artifact_events),
                },
            )
        return hints

    @staticmethod
    def _artifact_events_by_invocation(
        artifact_events: list[WebToolImageArtifactEvent],
    ) -> dict[int, list[WebToolImageArtifactEvent]]:
        grouped: dict[int, list[WebToolImageArtifactEvent]] = {}
        for event in sorted(artifact_events, key=lambda item: (item.invocation_id, item.sequence)):
            if event.invocation_id <= 0:
                continue
            grouped.setdefault(event.invocation_id, []).append(event)
        return grouped

    @staticmethod
    def _default_selected_image(
        *,
        raw_path: str,
        action_overlay_path: str,
        som_path: str,
    ) -> tuple[WebToolArtifactImageKind, str]:
        """Return the low-noise default image while preserving role-specific images.

        The complete tool artifact still carries raw, action-overlay, and SoM
        paths. ``selected_image`` is only the generic fallback for older
        consumers, so it should prefer the clean page state over debug-oriented
        overlays.
        """

        if raw_path:
            return WebToolArtifactImageKind.RAW, raw_path
        if action_overlay_path:
            return WebToolArtifactImageKind.ACTION_OVERLAY, action_overlay_path
        if som_path:
            return WebToolArtifactImageKind.SOM, som_path
        return WebToolArtifactImageKind.NONE, ""

    @staticmethod
    def _split_action_image_paths(image_paths: list[str]) -> tuple[str, str, str]:
        action_overlay_paths = [
            path for path in image_paths if WebArtifactRecordProjector._is_action_overlay_image_path(path)
        ]
        raw_paths = [
            path
            for path in image_paths
            if not WebArtifactRecordProjector._is_som_image_path(path)
            and not WebArtifactRecordProjector._is_action_overlay_image_path(path)
        ]
        som_paths = [path for path in image_paths if WebArtifactRecordProjector._is_som_image_path(path)]
        return (
            raw_paths[-1] if raw_paths else "",
            action_overlay_paths[-1] if action_overlay_paths else "",
            som_paths[-1] if som_paths else "",
        )

    def _existing_image_paths_from_output(self, output: JsonObject) -> list[Path]:
        paths: list[Path] = []
        seen: set[str] = set()
        for path_text in self._path_strings_from_output(output):
            path = Path(path_text)
            if not path.is_file():
                continue
            normalized = str(path)
            if normalized in seen:
                continue
            seen.add(normalized)
            paths.append(path)
        return paths

    def _path_strings_from_output(self, value: object) -> list[str]:
        paths: list[str] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key)
                if key_text in self._PATH_KEYS and isinstance(item, str) and item:
                    paths.append(item)
                    continue
                paths.extend(self._path_strings_from_output(item))
        elif isinstance(value, list):
            for item in value:
                paths.extend(self._path_strings_from_output(item))
        return paths

    def _image_paths_from_data_urls(
        self,
        output: JsonObject,
        digest_paths: Mapping[str, list[Path]],
    ) -> list[Path]:
        paths: list[Path] = []
        seen: set[str] = set()
        for data_url in self._image_data_urls_from_output(output):
            digest = self._digest_data_url(data_url)
            if not digest:
                continue
            matches = digest_paths.get(digest, [])
            if len(matches) != 1:
                continue
            normalized = str(matches[0])
            if normalized in seen:
                continue
            seen.add(normalized)
            paths.append(matches[0])
        return paths

    def _image_data_urls_from_output(self, value: object) -> list[str]:
        urls: list[str] = []
        if isinstance(value, Mapping):
            for key, item in value.items():
                key_text = str(key)
                if key_text in self._IMAGE_URL_KEYS and isinstance(item, str) and self._is_image_data_url(item):
                    urls.append(item)
                    continue
                urls.extend(self._image_data_urls_from_output(item))
        elif isinstance(value, list):
            for item in value:
                urls.extend(self._image_data_urls_from_output(item))
        return urls

    def _digest_paths(self, paths: list[Path]) -> dict[str, list[Path]]:
        digest_paths: dict[str, list[Path]] = {}
        for path in paths:
            try:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                continue
            digest_paths.setdefault(digest, []).append(path)
        return digest_paths

    def _digest_data_url(self, data_url: str) -> str:
        match = self._IMAGE_DATA_URL_PATTERN.fullmatch(data_url)
        if match is None:
            return ""
        try:
            data = base64.b64decode(match.group(1), validate=True)
        except (binascii.Error, ValueError):
            return ""
        return hashlib.sha256(data).hexdigest()

    @classmethod
    def _is_image_data_url(cls, value: str) -> bool:
        return bool(cls._IMAGE_DATA_URL_PATTERN.fullmatch(value))

    def _source_path_for_som(self, som_path: str) -> str:
        sidecar = Path(som_path).with_suffix(".annotation.json")
        if not sidecar.is_file():
            return ""
        try:
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ""
        if not isinstance(payload, dict):
            return ""
        source_path = payload.get("source_screenshot_path")
        if not isinstance(source_path, str) or not source_path:
            return ""
        return source_path if Path(source_path).is_file() else ""

    @staticmethod
    def _observation_summary(observation: object) -> str:
        if not isinstance(observation, dict):
            return ""
        page = observation.get("page")
        if not isinstance(page, dict):
            return ""
        title = str(page.get("title") or "(untitled)")
        url = str(page.get("url") or "(blank)")
        return f"{title} - {url}"

    @staticmethod
    def _som_metadata(screenshot: object) -> JsonObject:
        if not isinstance(screenshot, dict):
            return {}
        annotation = screenshot.get("annotation")
        if isinstance(annotation, dict):
            return cast(JsonObject, annotation)
        return {}

    @staticmethod
    def _is_som_image_path(path: str) -> bool:
        if not path:
            return False
        return path.endswith("_som.png") or "_som." in path or Path(path).with_suffix(".annotation.json").exists()

    @staticmethod
    def _is_action_overlay_image_path(path: str) -> bool:
        if not path:
            return False
        name = Path(path).name
        return (
            name.endswith("_action_overlay.png")
            or "_action_overlay." in name
            or name.endswith("_overlay.png")
            or "_overlay." in name
        ) and not WebArtifactRecordProjector._is_som_image_path(path)
