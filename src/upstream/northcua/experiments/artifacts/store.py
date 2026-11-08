"""Local artifact store abstraction for experiment outputs."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel


class ArtifactStore(Protocol):
    """Storage boundary used by verifiers and future object-store backends."""

    def put(self, *, source: Path, relative_path: str) -> str:
        """Store a local artifact and return its URI.

        Args:
            source: Existing local file to copy into the store.
            relative_path: Store-relative destination path.

        Returns:
            URI understood by this artifact store.
        """
        ...

    def exists(self, uri: str) -> bool:
        """Return whether an artifact URI exists.

        Args:
            uri: URI previously returned by ``put`` or indexed by verifier.

        Returns:
            ``True`` when the artifact can be resolved.
        """
        ...

    def resolve_uri(self, uri: str) -> Path:
        """Resolve a URI into a local path.

        Args:
            uri: Store URI.

        Returns:
            Local filesystem path for the artifact.
        """
        ...

    def list(self, prefix: str = "") -> list[str]:
        """List artifact URIs under a prefix.

        Args:
            prefix: Store-relative path prefix.

        Returns:
            Matching artifact URIs.
        """
        ...


class LocalExperimentArtifactStore:
    """Filesystem-backed artifact store rooted at an experiment directory."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def put(self, *, source: Path, relative_path: str) -> str:
        """Copy one file into the store and return a file URI.

        Args:
            source: Existing local file to copy.
            relative_path: Destination path relative to the store root.

        Returns:
            ``file://`` URI for the stored artifact.
        """

        destination = self._store_path(relative_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() != destination.resolve():
            shutil.copy2(source, destination)
        return destination.resolve().as_uri()

    def exists(self, uri: str) -> bool:
        """Return whether a local artifact URI or path exists.

        Args:
            uri: URI or local path to test.

        Returns:
            ``True`` when the resolved local file exists.
        """

        return self.resolve_uri(uri).exists()

    def resolve_uri(self, uri: str) -> Path:
        """Resolve a file URI or path into a local path.

        Args:
            uri: ``file://`` URI or already-local path.

        Returns:
            Local path represented by the URI.
        """

        if uri.startswith("file://"):
            return Path(uri.removeprefix("file://"))
        return Path(uri)

    def list(self, prefix: str = "") -> list[str]:
        """List file URIs under the local store root.

        Args:
            prefix: Optional store-relative prefix to limit the listing.

        Returns:
            Sorted artifact URIs for files below the prefix.
        """

        base = self._store_path(prefix) if prefix else self.root.resolve()
        if not base.exists():
            return []
        return [path.resolve().as_uri() for path in sorted(base.rglob("*")) if path.is_file()]

    def _store_path(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if candidate.is_absolute():
            raise ValueError("artifact store paths must be relative")
        root = self.root.resolve()
        resolved = (root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"artifact path escapes store root: {relative_path}") from exc
        return resolved


class ArtifactIndexRow(BaseModel):
    """One artifact entry used by dataset rows and audit reports.

    Attributes:
        task_id: Experiment task id.
        kind: Logical artifact kind such as ``screenshot`` or ``llm_trace``.
        uri: Store URI.
        path: Local path when the store is filesystem-backed.
        size_bytes: File size when known.
    """

    task_id: str
    kind: str
    uri: str
    path: str = ""
    size_bytes: int = 0


class ArtifactIndex:
    """Append and read typed artifact index rows."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, rows: list[ArtifactIndexRow]) -> None:
        """Overwrite the index with typed rows.

        Args:
            rows: Artifact rows to persist in JSONL form.
        """

        lines = [json.dumps(row.model_dump(mode="json"), ensure_ascii=False) for row in rows]
        self.path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def read(self) -> list[ArtifactIndexRow]:
        """Read all rows currently present in the index.

        Returns:
            Typed artifact index rows in file order.
        """

        if not self.path.exists():
            return []
        return [
            ArtifactIndexRow.model_validate_json(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
