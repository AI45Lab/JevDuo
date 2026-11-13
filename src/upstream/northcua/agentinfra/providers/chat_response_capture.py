"""Durable Chat completion receipts and a model-only empty-answer boundary."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

from northcua.core.json_types import JsonObject

_WRITE_LOCK = threading.Lock()


class EmptyModelResponseError(RuntimeError):
    """No usable assistant answer was produced within the configured attempts."""


class _ChatResponseAbort(BaseException):
    """Cross provider Exception retry loops; caught only by orchestration inference."""

    def __init__(self, error: Exception, *, retryable: bool = False) -> None:
        super().__init__(str(error))
        self.error, self.retryable = error, retryable


def chat_finish_reason(payload: JsonObject) -> str | None:
    choices = payload.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        reason = choices[0].get("finish_reason")
        return str(reason) if reason is not None else None
    return None


def _append_receipt(path: Path, record: JsonObject) -> None:
    encoded = (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    with _WRITE_LOCK, os.fdopen(os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600), "ab+") as stream:
        # OSWorld workers are POSIX; lock across processes as well as threads.
        if os.name == "posix":
            import fcntl

            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        os.fchmod(stream.fileno(), 0o600)
        stream.seek(0, os.SEEK_END)
        if stream.tell():
            stream.seek(-1, os.SEEK_END)
            if stream.read(1) != b"\n":
                raise RuntimeError("Truncated provider response log; preserve it and start a new session")
        stream.write(encoded)
        stream.flush()
        os.fsync(stream.fileno())


class ChatResponseCapture:
    """Record each complete SDK body before permitting normalization or retry."""

    def __init__(self, path: Path, metadata: JsonObject, attempt: int) -> None:
        self.path, self.metadata, self.attempt = path, metadata, attempt
        self.last_response: JsonObject = {}
        self.responses = 0

    def record_failure(self, error: BaseException, trace_state: object | None) -> None:
        """Retain projected input and any real response, without inventing usage."""
        request = getattr(trace_state, "request_payload", None)
        _append_receipt(self.path.with_name("provider_failures.jsonl"), {
            **self.metadata, "at": time.time(), "attempt": self.attempt,
            "error_type": type(error).__name__,
            "request_snapshot": request if isinstance(request, dict) and request else None,
            "request_scope": "provider projection snapshot; final transport may differ",
            "response": self.last_response or None,
            "usage": self.last_response.get("usage") if self.last_response else None,
            "request_delivery": "unknown",
        })

    def __call__(self, response: JsonObject) -> None:
        self.last_response = response
        self.responses += 1
        reason = chat_finish_reason(response)
        try:
            _append_receipt(
                self.path,
                {
                    **self.metadata,
                    "at": time.time(),
                    "attempt": self.attempt,
                    "response_index": self.responses,
                    "finish_reason": reason,
                    "response": response,
                },
            )
        except Exception as exc:
            # Never let NexAU repeat an inference because saving its receipt failed.
            raise _ChatResponseAbort(RuntimeError(f"Provider response capture failed: {exc}")) from exc
        choices = response.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            return  # Unexpected shapes belong to the provider/parser error path.
        message = choices[0].get("message")
        if not isinstance(message, dict):
            return
        content = message.get("content")
        if (isinstance(content, str) and content.strip()) or (content and not isinstance(content, str)):
            return
        if message.get("tool_calls") or message.get("function_call"):
            return
        # Refusals/filters are terminal provider decisions, not transient empty answers.
        retryable = not message.get("refusal") and reason != "content_filter"
        error = EmptyModelResponseError(
            f"Empty assistant content and no tool calls (attempt={self.attempt}, "
            f"finish_reason={reason or 'unknown'}, retryable={bool(retryable)}); response log: {self.path}"
        )
        raise _ChatResponseAbort(error, retryable=bool(retryable))
