"""Per-task evaluator mode selection for managed execution."""

from __future__ import annotations

from collections.abc import Mapping

EXTERNAL_SCRIPT_EVALUATION_MODE = "external_script"


def uses_external_script_evaluator(metadata: Mapping[str, object]) -> bool:
    """Return whether this task delegates reward evaluation to a script hook."""

    return metadata.get("evaluation_mode") == EXTERNAL_SCRIPT_EVALUATION_MODE


def disable_agentic_evaluator_for_external_script(app: object, metadata: Mapping[str, object]) -> bool:
    """Disable the app-owned LLM evaluator for an explicit script-only task."""

    if not uses_external_script_evaluator(metadata):
        return False
    app_config = getattr(app, "app_config", None)
    evaluation = getattr(app_config, "evaluation", None)
    if evaluation is None or not hasattr(evaluation, "enabled"):
        raise ValueError("external_script evaluation requires an app evaluation policy")
    evaluation.enabled = False
    return True


__all__ = [
    "EXTERNAL_SCRIPT_EVALUATION_MODE",
    "disable_agentic_evaluator_for_external_script",
    "uses_external_script_evaluator",
]
