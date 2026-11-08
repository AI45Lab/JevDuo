"""Batch manifest loading utilities."""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from northcua.batch.contracts import BatchManifest, BatchTaskSpec


def load_batch_manifest(path: str | Path) -> BatchManifest:
    """Load a YAML or JSONL batch manifest.

    YAML files may contain the full ``BatchManifest`` shape. JSONL files are
    interpreted as one ``BatchTaskSpec`` per line with default settings.

    Args:
        path: Manifest path.

    Returns:
        Validated manifest.

    Raises:
        ValueError: If the file suffix is unsupported.
    """

    resolved = Path(path).expanduser()
    suffix = resolved.suffix.lower()
    if suffix == ".json":
        raw = json.loads(resolved.read_text(encoding="utf-8"))
        return BatchManifest.model_validate(raw)
    if suffix in {".yaml", ".yml"}:
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        return BatchManifest.model_validate(raw)
    if suffix == ".jsonl":
        tasks = [
            BatchTaskSpec.model_validate(json.loads(line))
            for line in resolved.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        return BatchManifest(tasks=tasks)
    raise ValueError(f"Unsupported batch manifest suffix: {resolved.suffix}")
