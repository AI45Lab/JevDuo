"""Safe artifact path resolution for evaluator evidence."""

from __future__ import annotations

import re
from pathlib import Path

_REBASE_ANCHOR_DIRS = frozenset(
    {
        "accessibility",
        "a11y",
        "artifacts",
        "dom_snapshots",
        "screenshots",
        "steps",
    }
)
_QUERY_DIR_RE = re.compile(r"^query_\d{4}$")
_LEGACY_DESKTOP_TOP_LEVEL_ARTIFACT_RE = re.compile(
    r"^(?:"
    r"action_step\d+(?:_(?:desktop|verify|tool_\d+|probe_\d+|history_overlay_\d+))?\.png"
    r"|a11y_step\d+(?:_diff)?\.json"
    r"|a11y_step\d+\.raw\.(?:json|xml)"
    r")$"
)


class ArtifactPathResolver:
    """Resolve evaluator evidence paths under one artifact root."""

    def __init__(self, artifact_root: Path) -> None:
        self.artifact_root = artifact_root.resolve()

    def resolve(self, value: str) -> Path | None:
        """Resolve a path and reject values outside the artifact root.

        Args:
            value: Absolute or artifact-root-relative path.

        Returns:
            Resolved path when it stays inside the artifact root, otherwise
            `None`.
        """

        candidates = self._candidate_paths(value)
        for candidate in candidates:
            if self._is_under_artifact_root(candidate) and candidate.exists():
                return candidate
        for candidate in candidates:
            if self._is_under_artifact_root(candidate):
                return candidate
        return None

    def existing_file(self, value: str) -> Path | None:
        """Resolve a path and require it to be an existing file.

        Args:
            value: Absolute or artifact-root-relative path.

        Returns:
            Existing resolved file path, otherwise `None`.
        """

        for candidate in self._candidate_paths(value):
            if self._is_under_artifact_root(candidate) and candidate.is_file():
                return candidate
        return None

    def _candidate_paths(self, value: str) -> tuple[Path, ...]:
        path = Path(value)
        candidates: list[Path] = []
        if path.is_absolute():
            candidates.append(path.resolve())
            candidates.extend(self._rebased_artifact_suffix_candidates(path))
            return tuple(dict.fromkeys(candidates))
        candidates.append((self.artifact_root / path).resolve())
        cwd_candidate = (Path.cwd() / path).resolve()
        if cwd_candidate not in candidates:
            candidates.append(cwd_candidate)
        root_parts = self.artifact_root.parts
        path_parts = path.parts
        max_suffix_len = min(len(root_parts), len(path_parts))
        for suffix_len in range(max_suffix_len, 0, -1):
            if path_parts[:suffix_len] != root_parts[-suffix_len:]:
                continue
            stripped_candidate = self.artifact_root.joinpath(*path_parts[suffix_len:]).resolve()
            if stripped_candidate not in candidates:
                candidates.append(stripped_candidate)
        for index, part in enumerate(path_parts):
            if part != self.artifact_root.name:
                continue
            rebased_candidate = self.artifact_root.joinpath(*path_parts[index + 1 :]).resolve()
            if rebased_candidate not in candidates:
                candidates.append(rebased_candidate)
        return tuple(candidates)

    def _rebased_artifact_suffix_candidates(self, path: Path) -> list[Path]:
        """Return safe current-root candidates for copied artifact paths.

        Offline evaluator replay is often run from a copied artifact directory.
        Historical traces may still contain absolute paths from the original
        worktree. We never read those external files; instead, we only rebase
        known artifact subdirectory suffixes back under the supplied root.
        """

        candidates: list[Path] = []
        if _LEGACY_DESKTOP_TOP_LEVEL_ARTIFACT_RE.fullmatch(path.name):
            candidates.append((self.artifact_root / path.name).resolve())
        parts = path.parts
        for index, part in enumerate(parts):
            if part not in _REBASE_ANCHOR_DIRS and _QUERY_DIR_RE.fullmatch(part) is None:
                continue
            suffix = parts[index:]
            if len(suffix) < 2:
                continue
            candidate = self.artifact_root.joinpath(*suffix).resolve()
            if candidate not in candidates:
                candidates.append(candidate)
        return candidates

    def _is_under_artifact_root(self, candidate: Path) -> bool:
        try:
            candidate.relative_to(self.artifact_root)
        except ValueError:
            return False
        return True


__all__ = ["ArtifactPathResolver"]
