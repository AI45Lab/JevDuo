"""File access helpers for telemetry reports and trace JSONL files."""

from __future__ import annotations

import json
from pathlib import Path


class TelemetryReportStore:
    """Typed JSON/JSONL file access for telemetry report inputs and outputs."""

    def __init__(self, output_dir: Path) -> None:
        """Create a report store rooted at one batch output directory.

        Args:
            output_dir: Directory containing telemetry JSONL inputs and report outputs.
        """

        self.output_dir = output_dir

    def read_jsonl(self, filename: str) -> list[dict[str, object]]:
        """Read a report-local JSONL file, skipping malformed rows.

        Args:
            filename: File name relative to ``output_dir``.

        Returns:
            Parsed object rows; non-object and malformed rows are ignored.
        """

        return TelemetryJsonlReader.read_path(self.output_dir / filename)

    def write_json(self, filename: str, payload: object) -> None:
        """Write an indented JSON report.

        Args:
            filename: File name relative to ``output_dir``.
            payload: JSON-serializable payload to write.
        """

        (self.output_dir / filename).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_jsonl(self, filename: str, rows: list[dict[str, object]]) -> None:
        """Write JSONL report rows.

        Args:
            filename: File name relative to ``output_dir``.
            rows: JSON-serializable row payloads.
        """

        lines = [json.dumps(row, ensure_ascii=False) for row in rows]
        (self.output_dir / filename).write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")

    def write_text(self, filename: str, text: str) -> None:
        """Write a text report.

        Args:
            filename: File name relative to ``output_dir``.
            text: Report body.
        """

        (self.output_dir / filename).write_text(text, encoding="utf-8")


class TelemetryJsonlReader:
    """Robust JSONL reader for optional trace files."""

    @staticmethod
    def read_path(path: Path | None) -> list[dict[str, object]]:
        """Read an optional JSONL file into object rows.

        Args:
            path: Optional JSONL file path.

        Returns:
            Parsed object rows, or an empty list when the file is missing.
        """

        if path is None or not path.exists():
            return []
        rows: list[dict[str, object]] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict):
                rows.append(parsed)
        return rows
