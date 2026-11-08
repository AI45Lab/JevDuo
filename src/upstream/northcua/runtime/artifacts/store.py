"""Disk-backed artifact store for NorthCUA traces and screenshots."""

from __future__ import annotations

from pathlib import Path

from northcua.agentinfra.runtime.artifacts import AgentArtifactStoreBase
from northcua.core.config import ArtifactConfig
from northcua.core.enums import AccessibilityRawFormat


class NorthCUAArtifactStore(AgentArtifactStoreBase):
    """Own per-session paths and log append helpers."""

    def __init__(self, root: Path, config: ArtifactConfig, *, session_id: str | None = None) -> None:
        """Initialize a desktop artifact store with an active query scope.

        Args:
            root: Session-scoped artifact root directory.
            config: Artifact naming/layout config.
            session_id: Logical protocol/session identifier.
        """

        super().__init__(root, config, session_id=session_id)
        self._active_query_index = 1

    def set_query_index(self, query_index: int) -> None:
        """Set the default query directory used by step-level artifacts.

        Args:
            query_index: One-based user-query index within this session.
        """

        self._active_query_index = self._normalize_query_index(query_index)

    def query_dir(self, query_index: int | None = None) -> Path:
        """Return the artifact directory for one user-query turn.

        Args:
            query_index: Optional one-based user-query index. When omitted,
                the current active query index is used.

        Returns:
            Query-scoped artifact directory.
        """

        selected_index = self._active_query_index if query_index is None else query_index
        return self.root / f"query_{self._normalize_query_index(selected_index):04d}"

    @staticmethod
    def _normalize_query_index(query_index: int) -> int:
        """Return a safe one-based query index."""

        try:
            return max(1, int(query_index))
        except (TypeError, ValueError):
            return 1

    @property
    def response_log_path(self) -> Path:
        """Return the backward-compatible response log path.

        Returns:
            Per-session step log path.
        """

        return self.step_log_path

    @property
    def step_log_path(self) -> Path:
        """Return the per-session step log path.

        Returns:
            Path to the step JSONL log.
        """

        return self.root / self.config.step_log_name

    @property
    def llm_trace_log_path(self) -> Path:
        """Return the full-fidelity LLM trace path.

        Returns:
            Path to the per-session LLM trace JSONL file.
        """

        return self.root / self.config.llm_trace_log_name

    @property
    def primary_trace_path(self) -> Path:
        """Return the primary desktop trace file.

        Returns:
            Path to the primary trace file.
        """

        return self.step_log_path

    @property
    def evaluation_log_path(self) -> Path:
        """Return the per-session evaluation log path.

        Returns:
            Path to the evaluation log.
        """

        return self.root / self.config.evaluation_log_name

    @property
    def round_evaluation_log_path(self) -> Path:
        """Return the per-session round evaluation path.

        Returns:
            Path to the round evaluation JSONL file.
        """

        return self.root / self.config.round_evaluation_log_name

    @property
    def session_evaluation_rollup_path(self) -> Path:
        """Return the per-session evaluation rollup path.

        Returns:
            Path to the session evaluation rollup file.
        """

        return self.root / self.config.session_evaluation_rollup_name

    @property
    def structural_log_path(self) -> Path:
        """Return the backward-compatible structural log path.

        Returns:
            Per-session step log path.
        """

        return self.step_log_path

    @property
    def runtime_log_path(self) -> Path:
        """Return the structured runtime logger path.

        Returns:
            Path to the per-session runtime log.
        """

        return self.root / self.config.runtime_log_name

    @property
    def eval_runtime_log_path(self) -> Path:
        """Return the structured evaluator runtime logger path.

        Returns:
            Path to the per-session evaluator runtime log.
        """

        return self.root / self.config.eval_runtime_log_name

    def initial_screenshot_path(self, step_index: int) -> Path:
        """Return the initial or current screenshot path for a step.

        Args:
            step_index: Zero-based or runtime-provided step index.

        Returns:
            Screenshot path preserving the existing artifact naming scheme.
        """

        return self.query_dir() / f"action_step{step_index}.png"

    def desktop_screenshot_path(self, step_index: int) -> Path:
        """Return the raw desktop screenshot path for a step.

        Args:
            step_index: Zero-based or runtime-provided step index.

        Returns:
            Raw desktop screenshot path.
        """

        return self.query_dir() / f"action_step{step_index}_desktop.png"

    def overlay_path(self, step_index: int, overlay_index: int) -> Path:
        """Return the history overlay path for one retained image.

        Args:
            step_index: Zero-based or runtime-provided step index.
            overlay_index: Overlay sequence index for the step.

        Returns:
            Overlay image path.
        """

        return self.query_dir() / f"action_step{step_index}_history_overlay_{overlay_index}.png"

    def tool_screenshot_path(self, step_index: int, tool_index: int) -> Path:
        """Return the post-action screenshot path for one tool result.

        Args:
            step_index: Zero-based or runtime-provided step index.
            tool_index: Zero-based tool result index.

        Returns:
            Persisted post-action screenshot path.
        """

        return self.query_dir() / f"action_step{step_index}_tool_{tool_index + 1}.png"

    def probe_screenshot_path(self, step_index: int, probe_index: int) -> Path:
        """Return the screenshot path for one internal observation probe.

        Args:
            step_index: Zero-based or runtime-provided step index.
            probe_index: Zero-based probe result index.

        Returns:
            Persisted probe screenshot path.
        """

        return self.query_dir() / f"action_step{step_index}_probe_{probe_index + 1}.png"

    def verification_screenshot_path(self, step_index: int) -> Path:
        """Return the terminal verification screenshot path for one step.

        Args:
            step_index: One-based or runtime-provided step index.

        Returns:
            Persisted round-end verification screenshot path.
        """

        return self.query_dir() / f"action_step{step_index}_verify.png"

    def a11y_snapshot_path(self, step_index: int) -> Path:
        """Return the normalized accessibility snapshot path for one step.

        Args:
            step_index: Zero-based or runtime-provided step index.

        Returns:
            Persisted normalized accessibility snapshot path.
        """

        return self.query_dir() / f"a11y_step{step_index}.json"

    def a11y_raw_path(self, step_index: int, raw_format: AccessibilityRawFormat) -> Path:
        """Return the optional raw accessibility payload path for one step.

        Args:
            step_index: Zero-based or runtime-provided step index.
            raw_format: Raw payload format used to choose the extension.

        Returns:
            Persisted raw accessibility payload path.
        """

        extension = "xml" if raw_format == AccessibilityRawFormat.XML else "json"
        return self.query_dir() / f"a11y_step{step_index}.raw.{extension}"

    def a11y_diff_path(self, step_index: int) -> Path:
        """Return the accessibility diff path for one step.

        Args:
            step_index: Zero-based or runtime-provided step index.

        Returns:
            Persisted accessibility diff path.
        """

        return self.query_dir() / f"a11y_step{step_index}_diff.json"

    def eval_round_bundle_path(self, round_num: int) -> Path:
        """Return the evaluation bundle path for one round.

        Args:
            round_num: Evaluation round number.

        Returns:
            Persisted evaluation bundle path.
        """

        return self.root / f"eval_round_{round_num:04d}_bundle.json"

    def eval_round_rubric_path(self, round_num: int) -> Path:
        """Return the evaluation rubric path for one round.

        Args:
            round_num: Evaluation round number.

        Returns:
            Persisted evaluation rubric path.
        """

        return self.root / f"eval_round_{round_num:04d}_rubric.json"

    def eval_round_selection_path(self, round_num: int) -> Path:
        """Return the selected evidence path for one round.

        Args:
            round_num: Evaluation round number.

        Returns:
            Persisted selected evidence path.
        """

        return self.root / f"eval_round_{round_num:04d}_selection.json"

    def eval_round_result_path(self, round_num: int) -> Path:
        """Return the full evaluation result path for one round.

        Args:
            round_num: Evaluation round number.

        Returns:
            Persisted full evaluation result path.
        """

        return self.root / f"eval_round_{round_num:04d}_result.json"
