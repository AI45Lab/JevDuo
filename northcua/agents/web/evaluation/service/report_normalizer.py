"""Post-process Web evaluator reports with deterministic page-state fields."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from northcua.agentkernel.json_types import JsonObject
from northcua.agentkit.evaluator.artifacts import EvaluatorArtifactWriter


@dataclass(slots=True, frozen=True)
class WebVerifiedPageState:
    """Verified final browser page state extracted from terminal evidence."""

    url: str = ""
    title: str = ""
    source: str = ""
    session_step: int = 0


def normalize_web_final_report_page_state(
    *,
    writer: EvaluatorArtifactWriter,
    round_num: int,
    step_trace_by_session_step: Mapping[int, JsonObject],
) -> bool:
    """Copy verified terminal URL/title into the Web final report.

    The LLM judge decides the verdict and rationale, but Web-only
    ``final_url``/``final_title`` are factual page-state fields.  If terminal
    verification captured those strings, keep the persisted report exact so
    downstream QA and batch reports do not inherit harmless but noisy LLM
    rewrites such as singular/plural query parameter drift.
    """

    page_state = _verified_page_state(step_trace_by_session_step, round_num=round_num)
    if not page_state.url and not page_state.title:
        return False
    report_path = writer.layout.round_paths(round_num).final_report_path
    payload = _read_json_object(report_path)
    if payload is None:
        return False
    updates: JsonObject = {}
    if page_state.url and payload.get("final_url") != page_state.url:
        updates["final_url"] = page_state.url
    if page_state.title and payload.get("final_title") != page_state.title:
        updates["final_title"] = page_state.title
    if not updates:
        return False
    before = {key: payload.get(key) for key in updates}
    payload.update(updates)
    _write_json_object(report_path, payload)
    writer.append_runtime_event(
        round_num=round_num,
        payload={
            "event": "web_final_report_page_state_normalized",
            "source": page_state.source,
            "session_step": page_state.session_step,
            "updated_fields": sorted(updates),
            "before": before,
            "after": updates,
        },
    )
    return True


def _verified_page_state(
    step_trace_by_session_step: Mapping[int, JsonObject], *, round_num: int
) -> WebVerifiedPageState:
    for session_step, trace in sorted(step_trace_by_session_step.items(), reverse=True):
        if not isinstance(trace, dict):
            continue
        trace_round = _optional_int(trace.get("round_num"))
        if trace_round is not None and trace_round != round_num:
            continue
        terminal = trace.get("terminal_verification")
        if isinstance(terminal, dict) and terminal.get("status") == "captured":
            state = WebVerifiedPageState(
                url=str(terminal.get("page_url") or ""),
                title=str(terminal.get("page_title") or ""),
                source="terminal_verification",
                session_step=int(session_step),
            )
            if state.url or state.title:
                return state
    return WebVerifiedPageState()


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _read_json_object(path: Path) -> JsonObject | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_json_object(path: Path, payload: JsonObject) -> None:
    tmp_path = path.with_name(f".{path.name}.tmp")
    tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_path.replace(path)


__all__ = ["WebVerifiedPageState", "normalize_web_final_report_page_state"]
