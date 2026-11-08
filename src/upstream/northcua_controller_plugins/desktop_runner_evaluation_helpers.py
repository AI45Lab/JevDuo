"""Evaluator terminal-payload helpers for the desktop remote runner."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from northcua_protocol.capabilities import JsonObject
from northcua_protocol.payloads import TaskFinished

logger = logging.getLogger(__name__)


class DesktopRemoteEvaluationPayloadMixin:
    """Attach latest evaluator records to controller terminal payloads."""

    @staticmethod
    def _path_has_bytes(path: Path) -> bool:
        """Return whether a controller log artifact exists and is non-empty."""

        return path.exists() and path.is_file() and path.stat().st_size > 0

    @staticmethod
    def _with_latest_evaluation(payload: TaskFinished, artifact_store) -> TaskFinished:
        """Attach the latest evaluator record to the terminal payload if present."""

        evaluation = DesktopRemoteEvaluationPayloadMixin._latest_evaluation_record(artifact_store)
        if evaluation is None:
            return payload
        report, report_path = evaluation
        result = payload.result
        if isinstance(result, dict):
            enriched: JsonObject = dict(result)
        elif result is None:
            enriched = {}
        else:
            enriched = {"answer": result}
        if not any(
            isinstance(enriched.get(key), dict) for key in ("evaluation_report", "round_evaluation", "final_report")
        ):
            enriched["evaluation_report"] = report
        if report_path is not None and not (
            enriched.get("evaluation_report_path") or enriched.get("final_report_path")
        ):
            enriched["evaluation_report_path"] = str(report_path)
        return payload.model_copy(update={"result": _protocol_result(enriched)})

    @staticmethod
    def _latest_evaluation_record(artifact_store) -> tuple[JsonObject, Path | None] | None:
        record = DesktopRemoteEvaluationPayloadMixin._latest_round_evaluation_record(artifact_store)
        round_result_path = DesktopRemoteEvaluationPayloadMixin._latest_round_result_path(artifact_store, record=record)
        if round_result_path is not None and DesktopRemoteEvaluationPayloadMixin._path_has_bytes(round_result_path):
            result_payload = DesktopRemoteEvaluationPayloadMixin._read_json_file(round_result_path)
            if isinstance(result_payload, dict) and isinstance(result_payload.get("record"), dict):
                record = _json_object(result_payload["record"])
        if record is None:
            return None
        return record, round_result_path

    @staticmethod
    def _latest_round_result_path(artifact_store, *, record: JsonObject | None = None) -> Path | None:
        if record is None:
            record = DesktopRemoteEvaluationPayloadMixin._latest_round_evaluation_record(artifact_store)
        round_num = DesktopRemoteEvaluationPayloadMixin._round_num_from_record(record)
        if round_num is None or not hasattr(artifact_store, "eval_round_result_path"):
            return None
        return artifact_store.eval_round_result_path(round_num)

    @staticmethod
    def _latest_round_evaluation_record(artifact_store) -> JsonObject | None:
        path = getattr(artifact_store, "round_evaluation_log_path", None)
        if path is None or not DesktopRemoteEvaluationPayloadMixin._path_has_bytes(path):
            return None
        for raw_line in reversed(path.read_text(encoding="utf-8").splitlines()):
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Skipping malformed round evaluation record", extra={"path": str(path)})
                continue
            if isinstance(payload, dict):
                record = payload.get("record") if isinstance(payload.get("record"), dict) else payload
                return _json_object(record)
        return None

    @staticmethod
    def _round_num_from_record(record: JsonObject | None) -> int | None:
        if not record:
            return None
        value = record.get("round_num")
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.isdigit():
            parsed = int(value)
            return parsed if parsed > 0 else None
        return None

    @staticmethod
    def _read_json_file(path: Path) -> object:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("Could not read evaluator result payload", extra={"path": str(path)}, exc_info=True)
            return None


def _json_object(value: object) -> JsonObject:
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return {}


def _protocol_result(value: object | None) -> JsonObject | str | None:
    if value is None or isinstance(value, str):
        return value
    if isinstance(value, dict):
        return {str(key): item for key, item in value.items()}
    return str(value)
