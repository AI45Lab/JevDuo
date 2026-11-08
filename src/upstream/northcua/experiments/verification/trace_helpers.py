"""Path lookup and evaluator report helpers for trace verification."""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, UnidentifiedImageError
from PIL.Image import DecompressionBombError

from northcua.batch.contracts import BatchTaskState


def _part_matches_evaluator_kind(part: str, evaluator_kind: str) -> bool:
    return part == evaluator_kind or part.startswith(f"{evaluator_kind}_")


class ExperimentTraceVerifierHelpers:
    """Private artifact lookup, report parsing, and validation helpers."""

    def _find_first(
        self,
        name: str,
        *,
        state: BatchTaskState,
        roots: list[Path],
        exclude_evaluations: bool = False,
    ) -> Path | None:
        candidates = self._candidates(name, roots=roots, state=state)
        if exclude_evaluations:
            candidates = [path for path in candidates if "evaluations" not in path.parts]
        return candidates[0] if candidates else None

    def _find_evaluator_file(
        self,
        name: str,
        *,
        evaluator_kind: str,
        state: BatchTaskState,
        roots: list[Path],
    ) -> Path | None:
        candidates = [path for path in self._candidates(name, roots=roots, state=state) if "evaluations" in path.parts]
        preferred = [
            path
            for path in candidates
            if evaluator_kind and any(_part_matches_evaluator_kind(part, evaluator_kind) for part in path.parts)
        ]
        selected = preferred if evaluator_kind else candidates
        return selected[0] if selected else None

    def _find_screenshots(self, *, state: BatchTaskState, roots: list[Path]) -> list[Path]:
        paths: list[Path] = []
        for pattern in ("*.png", "*.jpg", "*.jpeg"):
            paths.extend(
                path
                for path in self._candidates(pattern, roots=roots, state=state, glob_pattern=True)
                if "evaluations" not in path.parts
            )
        local_layout = [
            path
            for path in paths
            if "query_" in str(path.parent)
            and "controller_command_artifacts" not in path.parts
            and state.session_id in path.parts
        ]
        return local_layout or paths

    @staticmethod
    def _candidates(
        name: str,
        *,
        roots: list[Path],
        state: BatchTaskState,
        glob_pattern: bool = False,
    ) -> list[Path]:
        tokens = [
            token for token in (state.session_id, state.trace_id, state.controller_task_id, state.task_id) if token
        ]
        matches: list[Path] = []
        for root in roots:
            if not root.exists():
                continue
            iterator = root.rglob(name) if glob_pattern else root.rglob(name)
            for path in iterator:
                if path.is_file() and (not tokens or any(token in str(path) for token in tokens)):
                    matches.append(path)
        return sorted({path.resolve(): path for path in matches}.values())

    @staticmethod
    def _non_empty(path: Path | None) -> bool:
        return path is not None and path.exists() and path.stat().st_size > 0

    @staticmethod
    def _artifact_parent(path: Path | None) -> str:
        return str(path.parent) if path is not None else ""

    @staticmethod
    def _dedupe_roots(roots: list[Path]) -> list[Path]:
        deduped: list[Path] = []
        seen: set[Path] = set()
        for root in roots:
            key = root.resolve()
            if key in seen:
                continue
            deduped.append(root)
            seen.add(key)
        return deduped

    @staticmethod
    def _evaluator_report_payload(path: Path | None) -> dict[str, object]:
        if path is None or not path.exists():
            return {}
        try:
            parsed = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _evaluator_verdict(payload: dict[str, object]) -> str:
        verdict = payload.get("overall_verdict")
        return verdict if isinstance(verdict, str) else ""

    @staticmethod
    def _evaluator_safety_risk_level(payload: dict[str, object]) -> str:
        safety = payload.get("safety")
        if not isinstance(safety, dict):
            return ""
        risk_level = safety.get("risk_level")
        return risk_level if isinstance(risk_level, str) else ""

    @staticmethod
    def _evaluator_efficiency_rating(payload: dict[str, object]) -> int:
        efficiency = payload.get("efficiency")
        if not isinstance(efficiency, dict):
            return 0
        rating = efficiency.get("rating")
        return rating if isinstance(rating, int) else 0

    @staticmethod
    def _evaluator_diagnosis_category_breakdown(payload: dict[str, object]) -> dict[str, int]:
        diagnoses = payload.get("diagnoses")
        if not isinstance(diagnoses, list):
            return {}
        counts: dict[str, int] = {}
        for diagnosis in diagnoses:
            if not isinstance(diagnosis, dict):
                continue
            category = diagnosis.get("category")
            if not isinstance(category, str) or not category:
                continue
            counts[category] = counts.get(category, 0) + 1
        return counts

    @staticmethod
    def _is_valid_image(path: Path) -> bool:
        try:
            with Image.open(path) as image:
                image.verify()
            with Image.open(path) as image:
                image.load()
        except (OSError, SyntaxError, UnidentifiedImageError, DecompressionBombError):
            return False
        return True
