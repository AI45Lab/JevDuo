"""Worker device-log metrics for batch health reports."""

from __future__ import annotations

import json
import os
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from northcua.maintenance.batch_health_time import parse_time_to_epoch as _parse_time_to_epoch

JsonDict = dict[str, object]

_WORKER_LOG_MAX_FILES = 5000
_WORKER_LOG_SAMPLE_LIMIT = 8
_WORKER_LOG_ERROR_LIMIT = 12


def worker_log_metrics(
    *,
    batch_db: Path,
    run_dir: Path,
    restart_ts: float,
    recent_window_minutes: int,
    warnings: list[str],
) -> JsonDict:
    """Summarize device worker logs for restart and recent windows.

    Args:
        batch_db: Batch SQLite path used to derive sandbox-log locations.
        run_dir: Case run directory that may contain sandbox logs.
        restart_ts: Restart epoch used for the since-restart window.
        recent_window_minutes: Recent rolling window size in minutes.
        warnings: Mutable warning list for scan/read issues.

    Returns:
        JSON-safe worker log summary with classifications, errors, and samples.
    """

    roots = _worker_log_roots(batch_db=batch_db, run_dir=run_dir)
    now = time.time()
    recent_cutoff = now - recent_window_minutes * 60
    scan_cutoffs = [recent_cutoff]
    if restart_ts > 0:
        scan_cutoffs.append(restart_ts)
    scan_cutoff = min(scan_cutoffs)
    files = _worker_log_files(roots=roots, scan_cutoff=scan_cutoff, warnings=warnings)
    found_total = len(files)
    files.sort(key=lambda item: item[0], reverse=True)
    truncated = found_total > _WORKER_LOG_MAX_FILES
    if truncated:
        warnings.append(
            f"worker log scan truncated: {found_total} files matched, newest {_WORKER_LOG_MAX_FILES} parsed"
        )
        files = files[:_WORKER_LOG_MAX_FILES]
    since_restart = _empty_worker_log_window()
    recent = _empty_worker_log_window()
    for mtime, _size, root, path in files:
        parsed = _parse_worker_device_log(path, root=root, mtime=mtime)
        last_seen = max(mtime, _float(parsed.get("last_event_ts_epoch")))
        if restart_ts <= 0 or last_seen >= restart_ts:
            _add_worker_log_window(since_restart, parsed)
        if last_seen >= recent_cutoff:
            _add_worker_log_window(recent, parsed)
    return {
        "roots": [str(root) for root in roots],
        "existing_roots": [str(root) for root in roots if root.exists()],
        "scan_cutoff": datetime.fromtimestamp(scan_cutoff, timezone.utc).isoformat(),
        "files_matched": found_total,
        "files_parsed": len(files),
        "truncated": truncated,
        "max_files": _WORKER_LOG_MAX_FILES,
        "since_restart": _finalize_worker_log_window(since_restart),
        "recent_window": _finalize_worker_log_window(recent),
    }


def _worker_log_roots(*, batch_db: Path, run_dir: Path) -> list[Path]:
    candidates = [
        batch_db.parent / "sandbox-logs",
        run_dir / "sandbox-logs",
        run_dir / "batch" / batch_db.parent.name / "sandbox-logs",
    ]
    roots: list[Path] = []
    seen: set[str] = set()
    for candidate in candidates:
        try:
            key = str(candidate.resolve())
        except OSError:
            key = str(candidate)
        if key in seen:
            continue
        seen.add(key)
        roots.append(candidate)
    return roots


def _worker_log_files(
    *,
    roots: list[Path],
    scan_cutoff: float,
    warnings: list[str],
) -> list[tuple[float, int, Path, Path]]:
    files: list[tuple[float, int, Path, Path]] = []
    for root in roots:
        if not root.exists():
            continue
        try:
            walker = os.walk(root)
            for dirpath, _dirnames, filenames in walker:
                for filename in filenames:
                    if not filename.endswith("device_runtime.jsonl"):
                        continue
                    path = Path(dirpath) / filename
                    try:
                        stat = path.stat()
                    except OSError as exc:
                        warnings.append(f"worker log stat failed: {path}: {type(exc).__name__}: {exc}")
                        continue
                    if stat.st_mtime < scan_cutoff:
                        continue
                    files.append((stat.st_mtime, stat.st_size, root, path))
        except OSError as exc:
            warnings.append(f"worker log scan failed: {root}: {type(exc).__name__}: {exc}")
    return files


def _empty_worker_log_window() -> JsonDict:
    return {
        "files": 0,
        "classification_counts": Counter(),
        "path_kind_counts": Counter(),
        "connection_error_counts": Counter(),
        "event_counts": Counter(),
        "samples": [],
    }


def _parse_worker_device_log(path: Path, *, root: Path, mtime: float) -> JsonDict:
    event_counts: Counter[str] = Counter()
    connection_errors: Counter[str] = Counter()
    first_error = ""
    first_ts = ""
    last_ts = ""
    first_ts_epoch = 0.0
    last_ts_epoch = 0.0
    device_id = ""
    server_url = ""
    sandbox_id = ""
    malformed_lines = 0
    read_error = ""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    malformed_lines += 1
                    continue
                if not isinstance(row, dict):
                    continue
                event = str(row.get("event") or row.get("message") or row.get("type") or "")
                if event:
                    event_counts[event] += 1
                timestamp = str(row.get("timestamp") or row.get("created_at") or row.get("time") or "")
                timestamp_epoch = _parse_time_to_epoch(timestamp)
                if timestamp_epoch > 0:
                    if first_ts_epoch <= 0 or timestamp_epoch < first_ts_epoch:
                        first_ts_epoch = timestamp_epoch
                        first_ts = timestamp
                    if timestamp_epoch >= last_ts_epoch:
                        last_ts_epoch = timestamp_epoch
                        last_ts = timestamp
                device_id = device_id or _worker_log_string(row, "device_id")
                server_url = server_url or _worker_log_string(row, "server_url")
                sandbox_id = sandbox_id or _worker_log_string(row, "sandbox_id")
                error = _worker_connection_error(row, event=event)
                if error:
                    first_error = first_error or error
                    connection_errors[_classify_worker_connection_error(error)] += 1
    except OSError as exc:
        read_error = f"{type(exc).__name__}: {exc}"
    if read_error:
        connection_errors["log_read_error"] += 1
        first_error = first_error or read_error
    classification = _classify_worker_log(event_counts)
    relative_path = _relative_path(path, root)
    return {
        "path": str(path),
        "relative_path": relative_path,
        "path_kind": _worker_log_path_kind(relative_path),
        "mtime": datetime.fromtimestamp(mtime, timezone.utc).isoformat(),
        "first_timestamp": first_ts,
        "last_timestamp": last_ts,
        "first_event_ts_epoch": first_ts_epoch,
        "last_event_ts_epoch": last_ts_epoch,
        "classification": classification,
        "event_counts": dict(event_counts.most_common(20)),
        "connection_error_counts": dict(connection_errors.most_common(_WORKER_LOG_ERROR_LIMIT)),
        "first_error": _shorten(first_error, 240),
        "device_id": device_id,
        "server_url": server_url,
        "sandbox_id": sandbox_id,
        "malformed_lines": malformed_lines,
        "read_error": read_error,
    }


def _worker_log_string(row: JsonDict, key: str) -> str:
    value = row.get(key)
    return value if isinstance(value, str) else ""


def _worker_connection_error(row: JsonDict, *, event: str) -> str:
    event_lower = event.lower()
    error = ""
    for key in ("error", "exception", "reason", "detail", "message"):
        value = row.get(key)
        if isinstance(value, str) and value:
            error = value
            break
    if not error:
        error = _find_string(row, "error") or _find_string(row, "exception") or _find_string(row, "reason")
    if error and (
        "error" in event_lower
        or "fail" in event_lower
        or "disconnect" in event_lower
        or "closed" in event_lower
        or any(token in error.lower() for token in ("websocket", "handshake", "http ", "close frame", "1006"))
    ):
        return _shorten(error, 500)
    return ""


def _classify_worker_connection_error(error: str) -> str:
    lowered = error.lower()
    if "timed out during opening handshake" in lowered:
        return "opening_handshake_timeout"
    if "http 530" in lowered:
        return "http_530"
    if "http 502" in lowered:
        return "http_502"
    if "http 504" in lowered:
        return "http_504"
    if "http 503" in lowered:
        return "http_503"
    if "no close frame" in lowered:
        return "no_close_frame"
    if "1006" in lowered or "abnormal closure" in lowered:
        return "abnormal_close_1006"
    if "connection refused" in lowered:
        return "connection_refused"
    if "name or service not known" in lowered or "temporary failure in name resolution" in lowered:
        return "dns_resolution"
    if "timed out" in lowered or "timeout" in lowered:
        return "timeout"
    return _shorten(error, 120)


def _classify_worker_log(event_counts: Counter[str]) -> str:
    connected = event_counts.get("connected", 0) > 0
    hello = (
        event_counts.get("device_hello_sent", 0) > 0
        or event_counts.get("device_registered", 0) > 0
        or event_counts.get("device_registration_sent", 0) > 0
    )
    lease = event_counts.get("lease_started", 0) > 0 or event_counts.get("task_accepted", 0) > 0
    command = (
        event_counts.get("device_command_received", 0) > 0
        or event_counts.get("command_received", 0) > 0
        or event_counts.get("command_started", 0) > 0
    )
    released = (
        event_counts.get("lease_released", 0) > 0
        or event_counts.get("command_result_sent", 0) > 0
        or event_counts.get("task_finished", 0) > 0
    )
    if not connected:
        return "never_opened"
    if not hello:
        return "connected_no_hello"
    if not lease:
        return "hello_no_lease"
    if not command:
        return "lease_no_command"
    if not released:
        return "command_unreleased"
    return "ran_released"


def _worker_log_path_kind(relative_path: str) -> str:
    lowered = relative_path.lower()
    if "prepared" in lowered:
        return "prepared"
    if "direct" in lowered:
        return "direct"
    return "unknown"


def _add_worker_log_window(window: JsonDict, parsed: JsonDict) -> None:
    window["files"] = int(window.get("files", 0)) + 1
    _counter(window, "classification_counts")[str(parsed.get("classification") or "unknown")] += 1
    _counter(window, "path_kind_counts")[str(parsed.get("path_kind") or "unknown")] += 1
    for key, value in dict(parsed.get("connection_error_counts", {})).items():
        _counter(window, "connection_error_counts")[str(key)] += _int(value)
    for key, value in dict(parsed.get("event_counts", {})).items():
        _counter(window, "event_counts")[str(key)] += _int(value)
    samples = window.get("samples")
    if isinstance(samples, list) and len(samples) < _WORKER_LOG_SAMPLE_LIMIT and _worker_log_should_sample(parsed):
        samples.append(
            {
                "classification": parsed.get("classification", ""),
                "path_kind": parsed.get("path_kind", ""),
                "relative_path": parsed.get("relative_path", ""),
                "mtime": parsed.get("mtime", ""),
                "first_timestamp": parsed.get("first_timestamp", ""),
                "last_timestamp": parsed.get("last_timestamp", ""),
                "device_id": parsed.get("device_id", ""),
                "sandbox_id": parsed.get("sandbox_id", ""),
                "first_error": parsed.get("first_error", ""),
                "events": parsed.get("event_counts", {}),
                "connection_errors": parsed.get("connection_error_counts", {}),
            }
        )


def _worker_log_should_sample(parsed: JsonDict) -> bool:
    classification = str(parsed.get("classification") or "")
    return classification != "ran_released" or bool(parsed.get("connection_error_counts"))


def _finalize_worker_log_window(window: JsonDict) -> JsonDict:
    return {
        "files": int(window.get("files", 0)),
        "classification_counts": dict(_counter(window, "classification_counts").most_common(20)),
        "path_kind_counts": dict(_counter(window, "path_kind_counts").most_common(10)),
        "connection_error_counts": dict(
            _counter(window, "connection_error_counts").most_common(_WORKER_LOG_ERROR_LIMIT)
        ),
        "event_counts": dict(_counter(window, "event_counts").most_common(20)),
        "samples": list(window.get("samples", []))[:_WORKER_LOG_SAMPLE_LIMIT],
    }


def _counter(value: JsonDict, key: str) -> Counter[str]:
    counter = value.get(key)
    if isinstance(counter, Counter):
        return counter
    replacement: Counter[str] = Counter()
    value[key] = replacement
    return replacement


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)


def _find_string(value: object, key: str) -> str:
    if isinstance(value, dict):
        item = value.get(key)
        if isinstance(item, str):
            return item
        for nested in value.values():
            found = _find_string(nested, key)
            if found:
                return found
    elif isinstance(value, list):
        for nested in value:
            found = _find_string(nested, key)
            if found:
                return found
    return ""


def _float(value: object) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return value[: max(limit - 3, 0)] + "..."
