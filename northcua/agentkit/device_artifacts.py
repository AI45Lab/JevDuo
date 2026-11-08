"""Artifact extraction helpers for AgentKit device command results."""

from __future__ import annotations

import base64
import mimetypes
import os
import re

from northcua_protocol.artifacts import ArtifactKind, ArtifactRef, ArtifactScope, RetentionPolicy

_IMAGE_DATA_URL_RE = re.compile(r"^data:(image/[-+.a-zA-Z0-9]+);base64,(.+)$")
DEFAULT_MAX_INLINE_IMAGE_ARTIFACT_BYTES = 10_000_000
_LOCAL_IMAGE_PATH_KEYS = {
    "screenshot_path": "screenshot_url",
    "image_path": "image_url",
    "annotated_screenshot_path": "annotated_screenshot_url",
    "action_overlay_image_path": "action_overlay_image_url",
}


class AgentKitCommandArtifactExtractor:
    """Extract inline artifacts from device command result payloads.

    The device data-plane may return screenshot-like data URLs nested inside a
    dict/list result. This helper replaces those bulky strings with artifact
    metadata and returns protocol artifact refs alongside the normalized value.
    """

    def __init__(self, *, max_inline_image_artifact_bytes: int = DEFAULT_MAX_INLINE_IMAGE_ARTIFACT_BYTES) -> None:
        """Initialize the extractor with the accepted image artifact cap.

        The default is intentionally aligned with ``WebAgentConfig.max_screenshot_bytes``.
        High-resolution Web screenshots can legitimately exceed the controller's
        small inline-task threshold, and controller persistence later compacts
        those bytes out of SQLite. Rejecting them here turns a valid browser tool
        result into an infrastructure command failure.
        """

        if max_inline_image_artifact_bytes <= 0:
            raise ValueError("max_inline_image_artifact_bytes must be positive")
        self.max_inline_image_artifact_bytes = max_inline_image_artifact_bytes

    def extract(self, command_id: str, value: object) -> tuple[object, list[ArtifactRef]]:
        """Extract image data URLs from a command result.

        Args:
            command_id: Command id used as a stable artifact-id prefix.
            value: Arbitrary normalized MCP result payload.

        Returns:
            Tuple of normalized payload and inline artifacts collected from it.
        """

        artifacts: list[ArtifactRef] = []
        prepared = self._attach_worker_local_image_urls(value)
        return self._extract_value(command_id=command_id, value=prepared, path="", artifacts=artifacts), artifacts

    def _attach_worker_local_image_urls(self, value: object) -> object:
        if isinstance(value, dict):
            normalized = {str(key): self._attach_worker_local_image_urls(child) for key, child in value.items()}
            for path_key, url_key in _LOCAL_IMAGE_PATH_KEYS.items():
                path = normalized.get(path_key)
                existing_url = normalized.get(url_key)
                if existing_url or not isinstance(path, str) or not path or not os.path.exists(path):
                    continue
                normalized[url_key] = self._inline_image_data_url(path)
            return normalized
        if isinstance(value, list):
            return [self._attach_worker_local_image_urls(child) for child in value]
        return value

    def _inline_image_data_url(self, path: str) -> str:
        size = os.path.getsize(path)
        if size > self.max_inline_image_artifact_bytes:
            raise ValueError(
                "worker-local image artifact exceeds AgentKit max inline artifact bytes "
                f"({size} > {self.max_inline_image_artifact_bytes})"
            )
        with open(path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
        mime_type = self._image_mime_type(path)
        return f"data:{mime_type};base64,{encoded}"

    @staticmethod
    def _image_mime_type(path: str) -> str:
        mime_type, _ = mimetypes.guess_type(path)
        if mime_type and mime_type.startswith("image/"):
            return mime_type
        return "image/png"

    def _extract_value(
        self,
        *,
        command_id: str,
        value: object,
        path: str,
        artifacts: list[ArtifactRef],
    ) -> object:
        if isinstance(value, dict):
            normalized: dict[str, object] = {}
            for key, child in value.items():
                child_path = f"{path}-{key}" if path else str(key)
                if isinstance(child, str):
                    normalized[str(key)] = child
                    self._replace_nested_artifact_metadata(
                        command_id=command_id,
                        key=str(key),
                        value=child,
                        path=child_path,
                        normalized=normalized,
                        artifacts=artifacts,
                    )
                    continue
                normalized[str(key)] = self._extract_value(
                    command_id=command_id,
                    value=child,
                    path=child_path,
                    artifacts=artifacts,
                )
            return normalized
        if isinstance(value, list):
            return [
                self._extract_value(command_id=command_id, value=child, path=f"{path}-{index}", artifacts=artifacts)
                for index, child in enumerate(value)
            ]
        if isinstance(value, str):
            return self._extract_string_artifact(command_id=command_id, value=value, path=path, artifacts=artifacts)
        return value

    def _replace_nested_artifact_metadata(
        self,
        *,
        command_id: str,
        key: str,
        value: str,
        path: str,
        normalized: dict[str, object],
        artifacts: list[ArtifactRef],
    ) -> None:
        match = _IMAGE_DATA_URL_RE.match(value)
        if match is None:
            return
        mime_type, encoded = match.groups()
        artifact = self._make_artifact(command_id=command_id, path=path, mime_type=mime_type, encoded=encoded)
        artifacts.append(artifact)
        normalized[key] = ""
        normalized[f"{key}_artifact_id"] = artifact.artifact_id
        normalized[f"{key}_artifact_sha256"] = artifact.sha256
        normalized[f"{key}_artifact_mime_type"] = artifact.mime_type

    def _extract_string_artifact(
        self,
        *,
        command_id: str,
        value: str,
        path: str,
        artifacts: list[ArtifactRef],
    ) -> object:
        match = _IMAGE_DATA_URL_RE.match(value)
        if match is None:
            return value
        mime_type, encoded = match.groups()
        artifacts.append(
            self._make_artifact(
                command_id=command_id,
                path=path or "artifact",
                mime_type=mime_type,
                encoded=encoded,
            )
        )
        return ""

    def _make_artifact(self, *, command_id: str, path: str, mime_type: str, encoded: str) -> ArtifactRef:
        return ArtifactRef.from_inline_bytes(
            artifact_id=self._artifact_id(command_id, path),
            kind=ArtifactKind.SCREENSHOT,
            data=self._decode_inline_artifact(encoded),
            mime_type=mime_type,
            artifact_scope=ArtifactScope.COMMAND,
            retention_policy=RetentionPolicy.TASK,
        )

    def _decode_inline_artifact(self, encoded: str) -> bytes:
        estimated_size = self._estimated_base64_decoded_size(encoded)
        if estimated_size > self.max_inline_image_artifact_bytes:
            raise ValueError(
                "inline artifact exceeds AgentKit max inline artifact bytes "
                f"(estimated {estimated_size} > {self.max_inline_image_artifact_bytes})"
            )
        data = base64.b64decode(encoded, validate=True)
        if len(data) > self.max_inline_image_artifact_bytes:
            raise ValueError(
                "inline artifact exceeds AgentKit max inline artifact bytes "
                f"({len(data)} > {self.max_inline_image_artifact_bytes})"
            )
        return data

    @staticmethod
    def _estimated_base64_decoded_size(encoded: str) -> int:
        padding = len(encoded) - len(encoded.rstrip("="))
        return max(0, (len(encoded) * 3 // 4) - padding)

    @staticmethod
    def _artifact_id(command_id: str, path: str) -> str:
        safe_path = re.sub(r"[^A-Za-z0-9_.-]+", "-", path).strip("-") or "artifact"
        return f"{command_id}-{safe_path}"
