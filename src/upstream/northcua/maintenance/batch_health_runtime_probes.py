"""Runtime probe helpers for batch health reports."""

from __future__ import annotations

import json
import subprocess
from collections import Counter
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

from northcua.maintenance.batch_health_time import parse_time_to_epoch as _parse_time_to_epoch

JsonDict = dict[str, object]


def _artifact_metrics(run_dir: Path, *, restart_ts: float) -> JsonDict:
    rows = _jsonl_rows(run_dir / "telemetry_events.jsonl")
    mime_counts: Counter[str] = Counter()
    for row in rows:
        if (
            restart_ts > 0
            and _parse_time_to_epoch(str(row.get("created_at") or row.get("timestamp") or "")) < restart_ts
        ):
            continue
        payload = row.get("payload")
        if not isinstance(payload, dict):
            payload = _loads(str(row.get("payload_json") or "{}"))
        if not payload:
            payload = row
        mime = _find_string(payload, "mime_type")
        if mime:
            mime_counts[mime] += 1
    return {"mime_counts_since_restart": dict(mime_counts)}


def _cloudflared_metrics(url: str, *, warnings: list[str]) -> JsonDict:
    try:
        with urlopen(url, timeout=3) as response:  # noqa: S310 - operator-provided local metrics URL.
            text = response.read().decode("utf-8", errors="replace")
    except (OSError, URLError) as exc:
        warnings.append(f"cloudflared metrics failed: {type(exc).__name__}: {exc}")
        return {}
    interest = {}
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        if any(
            key in line
            for key in (
                "cloudflared_tunnel_concurrent_requests_per_tunnel",
                "cloudflared_tunnel_request_errors",
                "cloudflared_tunnel_total_requests",
                "cloudflared_tunnel_response_by_code",
            )
        ):
            name, _, value = line.partition(" ")
            interest[name] = value.strip()
    return interest


def _socket_metrics(port: int, *, warnings: list[str]) -> JsonDict:
    try:
        completed = subprocess.run(
            ["ss", "-tan", f"sport = :{port}"],
            check=False,
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        warnings.append(f"socket metrics failed: {type(exc).__name__}: {exc}")
        return {}
    recv_total = send_total = max_recv = max_send = estab = 0
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 5 or parts[0] == "State":
            continue
        if parts[0] == "ESTAB":
            estab += 1
        recv = _int(parts[1])
        send = _int(parts[2])
        recv_total += recv
        send_total += send
        max_recv = max(max_recv, recv)
        max_send = max(max_send, send)
    return {
        "established": estab,
        "recv_q_total": recv_total,
        "send_q_total": send_total,
        "recv_q_max": max_recv,
        "send_q_max": max_send,
    }


def _jsonl_rows(path: Path) -> list[JsonDict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


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


def _loads(raw: str) -> JsonDict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _int(value: object) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
