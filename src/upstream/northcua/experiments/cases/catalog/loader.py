"""YAML loading for case app/template catalogs."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import yaml

from northcua.experiments.cases.catalog.registry import CaseCatalog


def default_case_catalog_path() -> Path:
    """Return the bundled E2B case catalog path.

    Returns:
        Absolute importlib resources path to the default catalog.
    """

    return Path(str(resources.files("northcua.experiments.cases.catalog.data") / "e2b-case-catalog.yaml"))


class CaseCatalogLoader:
    """Load typed app/template catalog entries from YAML."""

    def load(self, path: str | Path | None = None) -> CaseCatalog:
        """Load a catalog file.

        Args:
            path: Optional YAML path. When omitted, the bundled E2B catalog is used.

        Returns:
            Validated catalog registry.
        """

        resolved = (Path(path).expanduser() if path is not None else default_case_catalog_path()).resolve()
        raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
        return CaseCatalog.from_raw(raw, source_path=resolved)
