"""Load case suite and single-case specs from YAML or JSON."""

from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

from northcua.experiments.cases.contracts import (
    CaseFileSpec,
    CaseSourceKind,
    CaseSourceSpec,
    CaseSpec,
    CaseSuiteSpec,
    FixtureSpec,
)
from northcua_protocol.capabilities import JsonObject


def load_case_suite(path: str | Path) -> CaseSuiteSpec:
    """Load and validate a case suite spec.

    Args:
        path: YAML suite file that may embed tasks or reference case sources.

    Returns:
        Validated suite with external case sources expanded into tasks.
    """

    resolved = Path(path).expanduser().resolve()
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    if isinstance(raw, dict) and not raw.get("suite_id"):
        raw = {**raw, "suite_id": default_suite_id_for_path(resolved)}
    suite = CaseSuiteSpec.model_validate(raw)
    if not suite.case_sources:
        return suite
    expanded = [*suite.tasks]
    for source in suite.case_sources:
        expanded.extend(load_case_source(source, base_dir=resolved.parent))
    return CaseSuiteSpec.model_validate({**suite.model_dump(mode="python"), "case_sources": [], "tasks": expanded})


def load_case_file(path: str | Path) -> CaseSpec:
    """Load and validate one reusable case file.

    A case file may either use the explicit ``CaseFileSpec`` wrapper:

    ``version: v1`` / ``case: {...}``

    or a terse raw ``CaseSpec`` document with ``task_id`` at the top level.
    Relative fixture ``source`` paths are resolved against the case file's
    directory so cases can carry their own fixture folder.

    Args:
        path: YAML case file to load.

    Returns:
        Validated case with metadata indicating its source file.
    """

    resolved = Path(path).expanduser().resolve()
    raw = yaml.safe_load(resolved.read_text(encoding="utf-8")) or {}
    case, wrapper_metadata = _parse_case_document(raw)
    case = _resolve_case_fixture_paths(case, base_dir=resolved.parent)
    return case.model_copy(update={"metadata": {**wrapper_metadata, **case.metadata, "case_file": str(resolved)}})


def load_case_source(source: CaseSourceSpec, *, base_dir: Path) -> list[CaseSpec]:
    """Load one typed case source from a suite file.

    Args:
        source: File, directory, or JSONL source reference.
        base_dir: Directory used to resolve relative source paths.

    Returns:
        Cases loaded from the requested source.

    Raises:
        FileNotFoundError: When a referenced source path is missing.
        ValueError: When the source kind is unsupported.
    """

    if source.kind == CaseSourceKind.FILE:
        return [load_case_file(_resolve_source_path(source.path, base_dir=base_dir, expected="file"))]
    if source.kind == CaseSourceKind.DIRECTORY:
        directory = _resolve_source_path(source.path, base_dir=base_dir, expected="directory")
        iterator = directory.rglob(source.glob) if source.recursive else directory.glob(source.glob)
        paths = sorted(path for path in iterator if path.is_file())
        if not paths:
            raise FileNotFoundError(f"case directory matched no files: {directory} glob={source.glob!r}")
        return [load_case_file(path) for path in paths]
    if source.kind == CaseSourceKind.JSONL:
        return load_case_jsonl(_resolve_source_path(source.path, base_dir=base_dir, expected="file"))
    raise ValueError(f"Unsupported case source kind: {source.kind}")


def load_case_jsonl(path: str | Path) -> list[CaseSpec]:
    """Load JSONL where each non-empty line is one raw CaseSpec or CaseFileSpec.

    Args:
        path: JSONL file containing one case document per line.

    Returns:
        Validated cases with metadata indicating JSONL source and line.

    Raises:
        FileNotFoundError: When the JSONL file is missing.
        ValueError: When the file is empty or a line is invalid JSON.
    """

    resolved = Path(path).expanduser().resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"case JSONL not found: {resolved}")
    with resolved.open(encoding="utf-8") as handle:
        cases = [
            case
            for line_number, line in enumerate(handle, start=1)
            if (case := _load_case_jsonl_line(line, line_number=line_number, resolved=resolved)) is not None
        ]
    if not cases:
        raise ValueError(f"case JSONL contains no cases: {resolved}")
    return cases


def _load_case_jsonl_line(line: str, *, line_number: int, resolved: Path) -> CaseSpec | None:
    stripped = line.strip()
    if not stripped:
        return None
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid case JSONL at {resolved}:{line_number}: {exc}") from exc
    case, wrapper_metadata = _parse_case_document(raw)
    case = _resolve_case_fixture_paths(case, base_dir=resolved.parent)
    return case.model_copy(
        update={
            "metadata": {
                **wrapper_metadata,
                **case.metadata,
                "case_jsonl": str(resolved),
                "case_jsonl_line": line_number,
            }
        }
    )


def _parse_case_document(raw: object) -> tuple[CaseSpec, JsonObject]:
    if isinstance(raw, dict) and "case" in raw:
        wrapper = CaseFileSpec.model_validate(raw)
        return wrapper.case, wrapper.metadata
    return CaseSpec.model_validate(raw), {}


def _resolve_source_path(path: Path, *, base_dir: Path, expected: str) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        candidate = base_dir / candidate
    resolved = candidate.resolve()
    if expected == "file" and not resolved.is_file():
        raise FileNotFoundError(f"case source file not found: {resolved}")
    if expected == "directory" and not resolved.is_dir():
        raise FileNotFoundError(f"case source directory not found: {resolved}")
    return resolved


def _resolve_case_fixture_paths(case: CaseSpec, *, base_dir: Path) -> CaseSpec:
    fixtures, fixtures_changed = _resolve_fixture_list(case.fixtures, base_dir=base_dir)
    injection_files, injection_changed = _resolve_fixture_list(case.dependency_injection.files, base_dir=base_dir)
    if not fixtures_changed and not injection_changed:
        return case
    dependency_injection = case.dependency_injection
    if injection_changed:
        dependency_injection = dependency_injection.model_copy(update={"files": injection_files})
    return case.model_copy(update={"fixtures": fixtures, "dependency_injection": dependency_injection})


def _resolve_fixture_list(fixtures: list[FixtureSpec], *, base_dir: Path) -> tuple[list[FixtureSpec], bool]:
    resolved_fixtures: list[FixtureSpec] = []
    changed = False
    for fixture in fixtures:
        source_path = fixture.source_path
        if source_path is None or source_path.is_absolute():
            resolved_fixtures.append(fixture)
            continue
        resolved_fixtures.append(fixture.model_copy(update={"source_path": (base_dir / source_path).resolve()}))
        changed = True
    return resolved_fixtures, changed


def default_suite_id_for_path(path: Path) -> str:
    """Return the deterministic fallback suite id for a suite file path.

    Args:
        path: Suite spec path used to derive a slug.

    Returns:
        Path-safe suite id based on the file stem.
    """

    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", path.stem).strip("._-")
    return slug or "case-suite"


__all__ = ["default_suite_id_for_path", "load_case_file", "load_case_jsonl", "load_case_source", "load_case_suite"]
