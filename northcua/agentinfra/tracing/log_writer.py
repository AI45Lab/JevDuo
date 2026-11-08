"""Shared JSONL trace writers for agent-visible model payloads."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from pathlib import Path

from northcua.core.json_types import JsonObject, JsonValue


class JsonlTraceWriter:
    """Append JSON-serializable trace records to one JSONL file.

    Attributes:
        trace_path: JSONL trace file path.
    """

    def __init__(self, trace_path: Path) -> None:
        """Initialize the trace writer.

        Args:
            trace_path: JSONL trace file path.
        """

        self.trace_path = trace_path
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, payload: JsonObject) -> None:
        """Append one normalized trace payload.

        Args:
            payload: Trace payload to normalize and append.
        """

        normalized_payload = self._normalize_json_value(payload)
        with self.trace_path.open("a", encoding="utf-8") as handle:
            json.dump(normalized_payload, handle, ensure_ascii=False)
            handle.write("\n")

    def _normalize_json_value(self, value: object) -> JsonValue:
        """Convert complex runtime objects into plain JSON-friendly values."""

        if value is None or isinstance(value, (int, float, bool, str)):
            return value
        if isinstance(value, Path):
            return str(value)
        if isinstance(value, dict):
            return {str(key): self._normalize_json_value(nested) for key, nested in value.items()}
        if isinstance(value, (list, tuple, set)):
            return [self._normalize_json_value(item) for item in value]
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            try:
                return self._normalize_json_value(model_dump(mode="json"))
            except TypeError:
                return self._normalize_json_value(model_dump())
        if is_dataclass(value):
            return self._normalize_json_value(asdict(value))
        return str(value)


class LLMTraceLogWriter(JsonlTraceWriter):
    """Persist full-fidelity model traces to a dedicated JSONL file."""

    def log_desktop_trace(
        self,
        *,
        session_id: str,
        session_step: int,
        round_num: int,
        round_step: int,
        request: str,
        llm_trace: object,
    ) -> None:
        """Persist one desktop provider-facing trace record.

        Args:
            session_id: Session identifier.
            session_step: Session step number.
            round_num: Round number.
            round_step: Step number within the round.
            request: User request text.
            llm_trace: Full-fidelity LLM trace payload.
        """

        self.append(
            {
                "source": "desktop",
                "session_id": session_id,
                "session_step": session_step,
                "round_num": round_num,
                "round_step": round_step,
                "request": request,
                "llm_trace": llm_trace,
            }
        )

    def log_web_trace(
        self,
        *,
        session_id: str,
        session_step: int,
        round_num: int,
        round_step: int,
        request: str,
        llm_trace: object,
        runtime_cursor: JsonObject | None = None,
    ) -> None:
        """Persist one web provider-facing trace record.

        Args:
            session_id: Session identifier.
            session_step: Session step number.
            round_num: Round number.
            round_step: Step number within the round.
            request: User request text.
            llm_trace: Full-fidelity LLM trace payload.
            runtime_cursor: Optional shared runtime cursor payload for
                cross-runtime audit/debugging.
        """

        payload: JsonObject = {
            "source": "web",
            "model_request_stage": "logical_pre_provider_projection",
            "session_id": session_id,
            "session_step": session_step,
            "round_num": round_num,
            "round_step": round_step,
            "request": request,
            "llm_trace": llm_trace,
        }
        if runtime_cursor is not None:
            payload["runtime_cursor"] = runtime_cursor
        self.append(payload)
