"""Shared prompt rendering and JSON extraction helpers for evaluator phases."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from jinja2 import Environment, StrictUndefined
from pydantic import BaseModel, ValidationError

TModel = TypeVar("TModel", bound=BaseModel)


class EvaluationTemplateRenderer:
    """Small Jinja renderer for evaluator prompt templates."""

    def __init__(self, template_root: Path) -> None:
        """Initialize the evaluator template renderer.

        Args:
            template_root: Directory containing evaluator prompt templates.
        """

        self.template_root = template_root
        self.environment = Environment(
            autoescape=False,
            trim_blocks=True,
            lstrip_blocks=True,
            undefined=StrictUndefined,
        )

    def render(self, template_name: str, **context: object) -> str:
        """Render one evaluator template to text.

        Args:
            template_name: Filename under the template root.
            **context: Template variables passed to Jinja.

        Returns:
            Rendered template text with surrounding whitespace removed.
        """

        template_path = self.template_root / template_name
        template = self.environment.from_string(template_path.read_text(encoding="utf-8"))
        return template.render(**context).strip()


def parse_json_model(payload: str, model_type: type[TModel]) -> TModel:
    """Extract and validate one JSON object from an LLM response.

    Args:
        payload: Raw model response text.
        model_type: Pydantic model type to validate against.

    Returns:
        Parsed model instance.
    """

    candidate = _strip_fences(payload).strip()
    try:
        return model_type.model_validate_json(candidate)
    except ValidationError:
        extracted = _extract_first_json_object(candidate)
        return model_type.model_validate_json(extracted)


def _strip_fences(payload: str) -> str:
    """Remove a single Markdown code fence wrapper from an LLM response.

    Args:
        payload: Raw response text.

    Returns:
        Response text without the outer fence when present.
    """

    stripped = payload.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines)
    return stripped


def _extract_first_json_object(payload: str) -> str:
    """Extract the outermost JSON object from mixed response text.

    Args:
        payload: Response text that may include prose around JSON.

    Returns:
        The substring spanning the first ``{`` through the last ``}``.

    Raises:
        json.JSONDecodeError: If no plausible JSON object is present.
    """

    start = payload.find("{")
    end = payload.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise json.JSONDecodeError("No JSON object found", payload, 0)
    return payload[start : end + 1]
