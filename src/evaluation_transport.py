"""Check guest file-service reachability around official grading."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from uuid import uuid4

import requests


def _probe(session, endpoint):
    for attempt in range(2):
        try:
            # /file is a known read-only controller endpoint despite using POST.
            response = session.post(endpoint, data={"file_path": "/etc/os-release"}, timeout=30)
            try:
                response.raise_for_status()
                if not response.content:
                    raise ValueError("Empty guest file-service response")
            finally:
                response.close()
            return {"ok": True, "attempts": attempt + 1}
        except (requests.RequestException, ValueError) as error:
            last_error = type(error).__name__
    raise RuntimeError(f"Guest file service is unavailable during evaluation ({last_error})")


def evaluate_with_service_check(evaluate, *, endpoint: str, receipt: Path):
    report = {"call_id": uuid4().hex, "at": time.time(), "status": "error", "stage": "before"}
    try:
        with requests.Session() as session:
            session.trust_env = False
            report["before"] = _probe(session, endpoint)
            report["stage"] = "evaluate"
            raw = evaluate()
            report["unverified_raw"] = raw
            report["stage"] = "after"
            report["after"] = _probe(session, endpoint)
            report.update(status="verified", stage="complete")
            return raw
    except BaseException as error:
        report["error_type"] = type(error).__name__
        raise
    finally:
        receipt.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(receipt, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(descriptor, "a", encoding="utf-8") as stream:
            stream.write(json.dumps(report, ensure_ascii=False, default=str) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
