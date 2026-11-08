"""Remote command artifact materialization for Web runner steps."""

from __future__ import annotations

import base64
import json
import os
import re
from pathlib import Path

from northcua.core.config import WebAgentConfig
from northcua.core.json_types import JsonObject

from ..artifacts import WebArtifactStore

_IMAGE_DATA_URL_RE = re.compile(r"^data:(image/[-+.a-zA-Z0-9]+);base64,(.+)$", re.IGNORECASE)


class WebRemoteToolArtifactSink:
    """Persist remote inline image artifacts into the active Web artifact store."""

    def __init__(
        self,
        *,
        artifact_store: WebArtifactStore,
        config: WebAgentConfig,
        session_step: int,
    ) -> None:
        self.artifact_store = artifact_store
        self.config = config
        self.session_step = session_step
        self._counter = 0

    def materialize_image_data_url(
        self,
        *,
        tool_name: str,
        key: str,
        data_url: str,
        metadata: JsonObject,
    ) -> str:
        """Persist one remote inline image and return its local file path."""

        match = _IMAGE_DATA_URL_RE.match(data_url)
        if match is None:
            return ""
        mime_type, encoded = match.groups()
        estimated_size = (len(encoded) * 3) // 4
        if estimated_size > self.config.max_screenshot_bytes + 3:
            raise ValueError(
                f"Remote image exceeds web.max_screenshot_bytes={self.config.max_screenshot_bytes}: "
                f"estimated {estimated_size}"
            )
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > self.config.max_screenshot_bytes:
            raise ValueError(
                f"Remote image exceeds web.max_screenshot_bytes={self.config.max_screenshot_bytes}: {len(raw)}"
            )
        self._counter += 1
        annotation = _annotation_from_metadata(key=key, metadata=metadata)
        path = self._path_for(tool_name=tool_name, key=key, mime_type=mime_type, annotation=annotation)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(f"{path.suffix}.{os.getpid()}.tmp")
        tmp_path.write_bytes(raw)
        tmp_path.replace(path)
        if annotation:
            path.with_suffix(".annotation.json").write_text(
                json.dumps(annotation, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        return str(path)

    def _path_for(self, *, tool_name: str, key: str, mime_type: str, annotation: JsonObject) -> Path:
        suffix = ".jpg" if mime_type.lower() in {"image/jpeg", "image/jpg"} else ".png"
        safe_tool = _safe_segment(tool_name)
        safe_key = _safe_segment(key)
        kind_suffix = _image_kind_suffix(key=key, annotation=annotation)
        return (
            self.artifact_store.screenshots_dir
            / f"remote_tool_{self.session_step:03d}_{self._counter:03d}_{safe_tool}_{safe_key}{kind_suffix}{suffix}"
        )


def _safe_segment(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-._")
    return safe or "artifact"


def _annotation_from_metadata(*, key: str, metadata: JsonObject) -> JsonObject:
    if key not in {"som_image_url", "screenshot_url"}:
        return {}
    candidates: list[object] = []
    candidates.append(metadata.get("annotation"))
    result = metadata.get("result")
    if isinstance(result, dict):
        candidates.append(result.get("annotation"))
    observation = metadata.get("observation")
    if isinstance(observation, dict):
        screenshot = observation.get("screenshot")
        if isinstance(screenshot, dict):
            candidates.append(screenshot.get("annotation"))
    for candidate in candidates:
        if isinstance(candidate, dict) and candidate:
            return {str(key): value for key, value in candidate.items()}
    return {}


def _is_som_annotation(annotation: JsonObject) -> bool:
    return bool(annotation) and str(annotation.get("kind") or "set_of_mark") == "set_of_mark"


def _image_kind_suffix(*, key: str, annotation: JsonObject) -> str:
    if key == "action_overlay_image_url":
        return "_action_overlay"
    if key == "som_image_url":
        return "_som"
    if key == "screenshot_url" and _is_som_annotation(annotation):
        return "_som"
    return ""
