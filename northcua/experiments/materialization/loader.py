"""YAML loading for experiment specs."""

from __future__ import annotations

from pathlib import Path

import yaml

from northcua.experiments.contracts import ExperimentSpec


def load_experiment_spec(path: str | Path) -> ExperimentSpec:
    """Load and validate an experiment spec from YAML or JSON.

    Args:
        path: Spec path. Relative fixture references are left as authored;
            materialization resolves them against this file's parent.

    Returns:
        Validated experiment spec.

    Raises:
        ValueError: If the suffix is unsupported.
    """

    resolved = Path(path).expanduser().resolve()
    if resolved.suffix.lower() not in {".yaml", ".yml", ".json"}:
        raise ValueError(f"Unsupported experiment spec suffix: {resolved.suffix}")
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    return ExperimentSpec.model_validate(raw)
