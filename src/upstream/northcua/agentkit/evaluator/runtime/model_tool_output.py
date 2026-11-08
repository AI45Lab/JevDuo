"""Model-facing projection for evaluator tool outputs."""

from __future__ import annotations

import re
from collections.abc import Mapping

from northcua.agentkernel.json_types import JsonObject

_IMAGE_DATA_PREFIX = "data:image/"
_REDACTED_IMAGE_MARKER_PREFIX = "[redacted image data url;"
_IMAGE_PAYLOAD_KEY_RE = re.compile(r"(^|_)(image_url|screenshot_url|data_url)($|_)")
_PATH_KEY_RE = re.compile(r"(^|_)(path|paths|uri|storage_uri)($|_)")


class EvaluatorModelToolOutputProjector:
    """Project evaluator tool outputs before they are replayed to the model.

    Artifact tools can keep full debug payloads in ``tool_output``. The replayed
    ``llm_tool_output`` should stay compact and free of bulky media/path fields,
    because it is appended to the next evaluator prompt as tool history.
    """

    def project(self, value: object) -> object:
        """Return a model-facing copy of a tool output payload."""

        return self._project(value, key="")

    def _project(self, value: object, *, key: str) -> object:
        if isinstance(value, str):
            if self._is_image_payload_text(value):
                return "[image omitted]"
            return value
        if isinstance(value, Mapping):
            projected: JsonObject = {}
            for item_key, item in value.items():
                key_text = str(item_key)
                if self._is_omitted_key(key_text):
                    continue
                child = self._project(item, key=key_text)
                if child not in (None, "", [], {}):
                    projected[key_text] = child
            return projected
        if isinstance(value, list):
            projected_items = [self._project(item, key=key) for item in value]
            return [item for item in projected_items if item not in (None, "", [], {})]
        return value

    @staticmethod
    def _is_omitted_key(key: str) -> bool:
        lowered = key.lower()
        return bool(_IMAGE_PAYLOAD_KEY_RE.search(lowered)) or bool(_PATH_KEY_RE.search(lowered))

    @staticmethod
    def _is_image_payload_text(value: str) -> bool:
        lowered = value.strip().lower()
        return lowered.startswith(_IMAGE_DATA_PREFIX) or lowered.startswith(_REDACTED_IMAGE_MARKER_PREFIX)


__all__ = ["EvaluatorModelToolOutputProjector"]
